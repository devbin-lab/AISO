"""ComfyUI 로컬 API 클라이언트.

상태 조회부터 이미지 작업 제출·조회·취소까지 모든 요청은 검증된 HTTP
루프백 주소로만 보낸다. 리다이렉트와 환경 프록시는 사용하지 않으며 외부
응답 본문이나 traceback을 Aiso 오류에 그대로 포함하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import httpx


DEFAULT_COMFY_BASE_URL = "http://127.0.0.1:8188"
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REQUEST_TIMEOUT_SECONDS = 3.0
_MAX_WORKFLOW_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_NODE_INFO_BATCH = 64
_NODE_INFO_CONCURRENCY = 8
_MODEL_FOLDERS = frozenset(
    {
        "checkpoints",
        "diffusion_models",
        "text_encoders",
        "vae",
        "loras",
        "controlnet",
        "upscale_models",
    }
)
_NODE_CLASS_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
_ALL_JOB_STATUSES = frozenset({"pending", "in_progress", *_TERMINAL_JOB_STATUSES})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


class InvalidComfyURL(ValueError):
    """허용되지 않거나 잘못된 ComfyUI 주소."""


class ComfyAPIError(RuntimeError):
    """ComfyUI API 또는 응답 스키마 오류(외부 응답 본문을 포함하지 않음)."""


def normalize_base_url(value: str) -> str:
    """정확한 로컬 HTTP 루프백 URL만 허용하고 루트 주소로 정규화한다."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidComfyURL("ComfyUI 주소 형식이 올바르지 않습니다.")

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise InvalidComfyURL("ComfyUI 주소 형식이 올바르지 않습니다.") from exc

    if parsed.scheme.lower() != "http":
        raise InvalidComfyURL("ComfyUI 주소는 로컬 HTTP만 사용할 수 있습니다.")
    if not parsed.netloc:
        raise InvalidComfyURL("ComfyUI 주소에 로컬 호스트가 필요합니다.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise InvalidComfyURL("ComfyUI 주소에 사용자 정보는 사용할 수 없습니다.")
    # urlsplit은 빈 '?'·'#' 구분자를 빈 문자열로 접어 버리므로 원문도 함께 검사한다.
    if "?" in value or "#" in value or parsed.query or parsed.fragment:
        raise InvalidComfyURL("ComfyUI 주소에 쿼리나 프래그먼트는 사용할 수 없습니다.")
    if parsed.path not in ("", "/"):
        raise InvalidComfyURL("ComfyUI 주소는 서버 루트만 사용할 수 있습니다.")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InvalidComfyURL("ComfyUI 주소는 이 컴퓨터의 루프백 호스트만 사용할 수 있습니다.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidComfyURL("ComfyUI 포트 형식이 올바르지 않습니다.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise InvalidComfyURL("ComfyUI 포트 범위가 올바르지 않습니다.")

    rendered_host = "[::1]" if host == "::1" else host
    expected_authority = rendered_host if port is None else f"{rendered_host}:{port}"
    # urlsplit의 관대한 파싱(빈 포트, 비표준 authority 등)을 통과시키지 않는다.
    if parsed.netloc.lower() != expected_authority:
        raise InvalidComfyURL("ComfyUI 주소 형식이 올바르지 않습니다.")

    return f"http://{expected_authority}"


async def _request_json(
    base_url: str,
    method: str,
    route: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """검증이 끝난 base URL의 고정 route에 JSON 요청을 보낸다."""
    # 프록시 환경변수와 리다이렉트를 끈다. 검증된 루프백에서 다른 주소로 우회하지 않게 한다.
    async with httpx.AsyncClient(
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if method == "GET":
            response = await client.get(f"{base_url}{route}")
        elif method == "POST":
            if json_body is None:
                response = await client.post(f"{base_url}{route}")
            else:
                response = await client.post(f"{base_url}{route}", json=json_body)
        else:  # 공개 함수가 임의 HTTP 메서드를 통과시키지 않도록 내부에서도 닫아 둔다.
            raise ValueError("unsupported HTTP method")
        response.raise_for_status()
        return response.json()


async def _get_json(base_url: str, route: str) -> Any:
    return await _request_json(base_url, "GET", route)


def _canonical_uuid(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ComfyAPIError(f"{field} 형식이 올바르지 않습니다.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ComfyAPIError(f"{field} 형식이 올바르지 않습니다.") from exc
    if str(parsed) != value:
        raise ComfyAPIError(f"{field} 형식이 올바르지 않습니다.")
    return value


def _safe_relative(value: Any, *, allow_nested: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        raise ValueError("invalid relative path")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("absolute path")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError("invalid path segment")
    if not allow_nested and len(parts) != 1:
        raise ValueError("nested path is not allowed")
    return normalized


def _normalize_output_refs(outputs: Any) -> list[dict[str, Any]]:
    if not isinstance(outputs, dict):
        raise ValueError("outputs must be an object")
    refs: list[dict[str, Any]] = []
    for node_id, node_output in outputs.items():
        if not isinstance(node_id, str) or not isinstance(node_output, dict):
            raise ValueError("invalid node output")
        images = node_output.get("images", [])
        if not isinstance(images, list):
            raise ValueError("images must be a list")
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                raise ValueError("image reference must be an object")
            filename = _safe_relative(item.get("filename"), allow_nested=False)
            suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if suffix not in _IMAGE_EXTENSIONS:
                raise ValueError("unsupported image extension")
            subfolder_raw = item.get("subfolder", "")
            if subfolder_raw == "":
                subfolder = ""
            else:
                subfolder = _safe_relative(subfolder_raw, allow_nested=True)
            storage_type = item.get("type")
            if storage_type != "output":
                raise ValueError("unsupported output storage type")
            refs.append(
                {
                    "nodeId": node_id,
                    "index": index,
                    "filename": filename,
                    "subfolder": subfolder,
                    "storageType": "output",
                }
            )
            if len(refs) > 8:
                raise ValueError("too many output images")
    return refs


def _safe_execution_error(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return {"type": "execution_error", "message": "ComfyUI 이미지 생성에 실패했습니다."}
    error_type = value.get("exception_type") or value.get("type") or "execution_error"
    node_type = value.get("node_type")
    result = {
        "type": str(error_type)[:100],
        "message": "ComfyUI 이미지 생성에 실패했습니다.",
    }
    if isinstance(node_type, str) and _NODE_CLASS_RE.fullmatch(node_type):
        result["nodeType"] = node_type
    return result


def _request_error_detail(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "ComfyUI 응답 시간이 초과되었습니다."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"ComfyUI가 HTTP {exc.response.status_code} 오류를 반환했습니다."
    return "ComfyUI에 연결할 수 없습니다."


def _frontend_version(system: dict[str, Any]) -> str | None:
    direct = (
        system.get("frontend_version")
        or system.get("installed_frontend_version")
        or system.get("required_frontend_version")
    )
    if isinstance(direct, str):
        return direct

    packages = system.get("comfy_package_versions")
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, dict):
                continue
            if package.get("name") == "comfyui-frontend-package":
                installed = package.get("installed")
                if isinstance(installed, str):
                    return installed
    return None


def _normalize_devices(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("devices must be a list")

    devices: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("device must be an object")
        name = item.get("name")
        device_type = item.get("type")
        vram_total = item.get("vram_total")
        vram_free = item.get("vram_free")
        if not isinstance(name, str) or not isinstance(device_type, str):
            raise ValueError("device identity must be text")
        if not isinstance(vram_total, int) or isinstance(vram_total, bool):
            raise ValueError("vram_total must be an integer")
        if not isinstance(vram_free, int) or isinstance(vram_free, bool):
            raise ValueError("vram_free must be an integer")
        devices.append(
            {
                "name": name,
                "type": device_type,
                "vram_total": vram_total,
                "vram_free": vram_free,
            }
        )
    return devices


def _offline(base_url: str, detail: str) -> dict[str, Any]:
    return {
        "online": False,
        "base_url": base_url,
        "version": None,
        "frontend_version": None,
        "devices": [],
        "detail": detail,
    }


async def get_health(base_url: str = DEFAULT_COMFY_BASE_URL) -> dict[str, Any]:
    """ComfyUI 상태를 안정된 Aiso 응답 형태로 정규화한다."""
    normalized = normalize_base_url(base_url)
    try:
        payload = await _get_json(normalized, "/system_stats")
    except httpx.HTTPError as exc:
        return _offline(normalized, _request_error_detail(exc))
    except (TypeError, ValueError):
        return _offline(normalized, "ComfyUI 응답 형식이 올바르지 않습니다.")

    try:
        if not isinstance(payload, dict):
            raise ValueError("response must be an object")
        system = payload.get("system")
        if not isinstance(system, dict):
            raise ValueError("system must be an object")
        version = system.get("comfyui_version")
        if not isinstance(version, str) or not version:
            raise ValueError("version must be text")
        devices = _normalize_devices(payload.get("devices"))
        return {
            "online": True,
            "base_url": normalized,
            "version": version,
            "frontend_version": _frontend_version(system),
            "devices": devices,
            "detail": None,
        }
    except (TypeError, ValueError):
        return _offline(normalized, "ComfyUI 응답 형식이 올바르지 않습니다.")


async def get_checkpoints(base_url: str = DEFAULT_COMFY_BASE_URL) -> dict[str, Any]:
    """ComfyUI가 노출하는 체크포인트 이름을 정렬·중복 제거해 반환한다."""
    normalized = normalize_base_url(base_url)
    try:
        payload = await _get_json(normalized, "/models/checkpoints")
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 응답 형식이 올바르지 않습니다.") from exc

    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ComfyAPIError("ComfyUI 체크포인트 응답 형식이 올바르지 않습니다.")

    return {
        "base_url": normalized,
        "checkpoints": sorted(set(payload)),
    }


async def get_model_files(base_url: str, folder: str) -> list[str]:
    """허용된 ComfyUI 모델 폴더의 상대 이름 목록을 반환한다."""
    normalized = normalize_base_url(base_url)
    if folder not in _MODEL_FOLDERS:
        raise ComfyAPIError("지원하지 않는 ComfyUI 모델 폴더입니다.")
    try:
        payload = await _get_json(normalized, f"/models/{quote(folder, safe='')}")
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 모델 목록 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ComfyAPIError("ComfyUI 모델 목록 응답 형식이 올바르지 않습니다.")
    return sorted(set(payload))


async def get_models_inventory(
    base_url: str,
    folders: set[str] | frozenset[str] | tuple[str, ...] | list[str],
) -> dict[str, list[str]]:
    """생성 프로필 검증에 필요한 모델 폴더만 조회한다."""
    requested = sorted(set(folders))
    if not requested or any(folder not in _MODEL_FOLDERS for folder in requested):
        raise ComfyAPIError("지원하지 않는 ComfyUI 모델 폴더입니다.")
    inventory: dict[str, list[str]] = {}
    for folder in requested:
        inventory[folder] = await get_model_files(base_url, folder)
    return inventory


async def get_node_info(base_url: str, node_class: str) -> dict[str, Any]:
    """신뢰된 템플릿이 요구하는 단일 노드 계약을 조회한다."""
    normalized = normalize_base_url(base_url)
    if not isinstance(node_class, str) or not _NODE_CLASS_RE.fullmatch(node_class):
        raise ComfyAPIError("ComfyUI 노드 이름 형식이 올바르지 않습니다.")
    try:
        payload = await _get_json(normalized, f"/object_info/{quote(node_class, safe='')}")
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 노드 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get(node_class), dict):
        raise ComfyAPIError(f"필수 ComfyUI 노드가 없습니다: {node_class}")
    return payload[node_class]


async def get_node_infos(base_url: str, node_classes: Any) -> dict[str, dict[str, Any]]:
    """Fetch a bounded set of node contracts concurrently.

    ComfyUI exposes each node through ``/object_info/<class>``.  A workflow
    can contain several distinct core nodes, so serial three-second requests
    make an otherwise valid workflow appear hung.  The bounded fan-out keeps a
    local server responsive and avoids an unbounded request burst.
    """
    if isinstance(node_classes, (str, bytes)):
        raise ComfyAPIError("ComfyUI 노드 목록 형식이 올바르지 않습니다.")
    try:
        classes = sorted(set(node_classes))
    except TypeError as exc:
        raise ComfyAPIError("ComfyUI 노드 목록 형식이 올바르지 않습니다.") from exc
    if not classes or len(classes) > _MAX_NODE_INFO_BATCH or any(
        not isinstance(node_class, str) or not _NODE_CLASS_RE.fullmatch(node_class)
        for node_class in classes
    ):
        raise ComfyAPIError("ComfyUI 노드 계약 조회 범위를 벗어났습니다.")
    semaphore = asyncio.Semaphore(_NODE_INFO_CONCURRENCY)

    async def load(node_class: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return node_class, await get_node_info(base_url, node_class)

    pairs = await asyncio.gather(*(load(node_class) for node_class in classes))
    return dict(pairs)


async def get_jobs_capability(base_url: str) -> dict[str, Any]:
    """추적·단일 취소가 가능한 ComfyUI 0.28 local jobs API를 사전 확인한다."""
    normalized = normalize_base_url(base_url)
    try:
        payload = await _get_json(normalized, "/api/jobs?limit=1")
    except httpx.HTTPError as exc:
        # 연결 거부·timeout까지 API 미지원으로 표시하면 사용자가 설치 호환성 문제로
        # 오인한다. 실제 route 부재를 뜻하는 상태만 미지원으로 분류한다.
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
            404,
            405,
            501,
        }:
            raise ComfyAPIError(
                "현재 ComfyUI는 안전한 단일 작업 추적·취소 API를 지원하지 않습니다."
            ) from exc
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 작업 추적 API 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ComfyAPIError("ComfyUI 작업 추적 API 응답 형식이 올바르지 않습니다.")
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        raise ComfyAPIError("ComfyUI 작업 추적 API 응답 형식이 올바르지 않습니다.")
    total = pagination.get("total")
    has_more = pagination.get("has_more")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or not isinstance(has_more, bool)
    ):
        raise ComfyAPIError("ComfyUI 작업 추적 API 응답 형식이 올바르지 않습니다.")
    return {"supported": True, "baseUrl": normalized}


async def submit_prompt(
    base_url: str,
    workflow: dict[str, Any],
    *,
    client_id: str,
    prompt_id: str,
) -> dict[str, Any]:
    """검증된 API 형식 워크플로를 client-generated UUID로 제출한다."""
    normalized = normalize_base_url(base_url)
    client_id = _canonical_uuid(client_id, "ComfyUI client ID")
    prompt_id = _canonical_uuid(prompt_id, "ComfyUI prompt ID")
    if not isinstance(workflow, dict) or not workflow:
        raise ComfyAPIError("ComfyUI 워크플로 형식이 올바르지 않습니다.")
    try:
        encoded = json.dumps(
            workflow,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 워크플로 형식이 올바르지 않습니다.") from exc
    if len(encoded) > _MAX_WORKFLOW_BYTES:
        raise ComfyAPIError("ComfyUI 워크플로 크기가 제한을 초과했습니다.")
    try:
        payload = await _request_json(
            normalized,
            "POST",
            "/prompt",
            json_body={"prompt": workflow, "client_id": client_id, "prompt_id": prompt_id},
        )
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 작업 제출 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, dict) or payload.get("prompt_id") != prompt_id:
        raise ComfyAPIError("ComfyUI 작업 제출 응답 형식이 올바르지 않습니다.")
    number = payload.get("number")
    node_errors = payload.get("node_errors")
    if (
        not isinstance(number, (int, float))
        or isinstance(number, bool)
        or not math.isfinite(float(number))
        or not isinstance(node_errors, dict)
    ):
        raise ComfyAPIError("ComfyUI 작업 제출 응답 형식이 올바르지 않습니다.")
    return {"promptId": prompt_id, "queueNumber": number, "nodeErrors": node_errors}


async def get_job(base_url: str, prompt_id: str) -> dict[str, Any]:
    """ComfyUI 0.28 local jobs API 응답을 Aiso 작업 상태로 정규화한다."""
    normalized = normalize_base_url(base_url)
    prompt_id = _canonical_uuid(prompt_id, "ComfyUI prompt ID")
    try:
        payload = await _get_json(normalized, f"/api/jobs/{quote(prompt_id, safe='')}")
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 작업 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, dict) or payload.get("id") != prompt_id:
        raise ComfyAPIError("ComfyUI 작업 응답 형식이 올바르지 않습니다.")
    status = payload.get("status")
    if status not in _ALL_JOB_STATUSES:
        raise ComfyAPIError("ComfyUI 작업 상태가 올바르지 않습니다.")
    try:
        outputs = _normalize_output_refs(payload.get("outputs", {})) if status == "completed" else []
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 결과 이미지 설명자가 올바르지 않습니다.") from exc
    return {
        "promptId": prompt_id,
        "status": status,
        "terminal": status in _TERMINAL_JOB_STATUSES,
        "outputs": outputs,
        "error": _safe_execution_error(payload.get("execution_error")) if status == "failed" else None,
    }


async def cancel_job(base_url: str, prompt_id: str) -> dict[str, Any]:
    """ComfyUI 0.28의 단일 작업 취소만 사용한다. 전역 interrupt fallback은 없다."""
    normalized = normalize_base_url(base_url)
    prompt_id = _canonical_uuid(prompt_id, "ComfyUI prompt ID")
    try:
        payload = await _request_json(
            normalized,
            "POST",
            f"/api/jobs/{quote(prompt_id, safe='')}/cancel",
        )
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 작업 취소 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cancelled"), bool):
        raise ComfyAPIError("ComfyUI 작업 취소 응답 형식이 올바르지 않습니다.")
    return {"promptId": prompt_id, "cancelled": payload["cancelled"]}


async def release_models(base_url: str) -> dict[str, bool]:
    """완료된 생성 뒤 ComfyUI 모델과 캐시의 VRAM 해제를 요청한다.

    ComfyUI ``/free``는 성공 시 빈 200 응답을 반환하므로 JSON 공통 경로와
    분리한다. 호출자는 이미지 결과를 보존하기 위해 이 오류를 best-effort로
    다룰 수 있다.
    """
    normalized = normalize_base_url(base_url)
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{normalized}/free",
                json={"unload_models": True, "free_memory": True},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc
    return {"requested": True}


async def fetch_output_image(
    base_url: str,
    filename: str,
    subfolder: str = "",
    storage_type: str = "output",
) -> tuple[bytes, str]:
    """검증된 ComfyUI /view 이미지 한 장을 최대 50 MiB까지 중계한다."""
    normalized = normalize_base_url(base_url)
    try:
        safe_filename = _safe_relative(filename, allow_nested=False)
        suffix = "." + safe_filename.rsplit(".", 1)[-1].lower() if "." in safe_filename else ""
        if suffix not in _IMAGE_EXTENSIONS:
            raise ValueError("unsupported image extension")
        safe_subfolder = "" if subfolder == "" else _safe_relative(subfolder, allow_nested=True)
    except (TypeError, ValueError) as exc:
        raise ComfyAPIError("ComfyUI 결과 이미지 경로가 올바르지 않습니다.") from exc
    if storage_type not in {"output", "temp"}:
        raise ComfyAPIError("ComfyUI 결과 이미지 저장 유형이 올바르지 않습니다.")

    params = {"filename": safe_filename, "subfolder": safe_subfolder, "type": storage_type}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=_REQUEST_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", f"{normalized}/view", params=params) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                expected_types = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                }
                if not content_type.startswith("image/") or content_type != expected_types[suffix]:
                    raise ComfyAPIError("ComfyUI 결과가 허용된 이미지 형식이 아닙니다.")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError as exc:
                        raise ComfyAPIError("ComfyUI 이미지 크기 응답이 올바르지 않습니다.") from exc
                    if declared < 0 or declared > _MAX_IMAGE_BYTES:
                        raise ComfyAPIError("ComfyUI 결과 이미지가 50 MiB 제한을 초과했습니다.")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    if not isinstance(chunk, bytes):
                        raise ComfyAPIError("ComfyUI 이미지 응답 형식이 올바르지 않습니다.")
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        raise ComfyAPIError("ComfyUI 결과 이미지가 50 MiB 제한을 초과했습니다.")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    except ComfyAPIError:
        raise
    except httpx.HTTPError as exc:
        raise ComfyAPIError(_request_error_detail(exc)) from exc


__all__ = [
    "ComfyAPIError",
    "DEFAULT_COMFY_BASE_URL",
    "InvalidComfyURL",
    "cancel_job",
    "fetch_output_image",
    "get_checkpoints",
    "get_health",
    "get_job",
    "get_jobs_capability",
    "get_model_files",
    "get_models_inventory",
    "get_node_info",
    "get_node_infos",
    "normalize_base_url",
    "release_models",
    "submit_prompt",
]
