"""사용자 등록 모델을 선택해 ComfyUI 0.28 작업을 안전하게 실행한다."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

import comfy_client
from comfy_workflows import (
    ARCH_FLUX1_SPLIT,
    ARCH_FLUX2_KLEIN_4B,
    MAX_PROMPT_LENGTH,
    WorkflowValidationError,
    apply_prompt_policy,
    build_workflow,
    inventory_folders,
    node_classes_for_profile,
    normalize_profiles,
    primary_model_name,
    required_assets,
    resolve_generation_options,
    select_profile,
    snapshot_workflow,
    validate_runtime_options,
)


POLL_INTERVAL_SECONDS = 0.5
GENERATION_TIMEOUT_SECONDS = 30 * 60
# Global /free evicts the models used by the user's own ComfyUI window too.
# Keep it opt-in for constrained/headless deployments; interactive Aiso never
# unloads models behind a user's back.
RELEASE_MODELS_AFTER_AISO_JOB = os.environ.get("AISO_COMFY_RELEASE_MODELS", "").strip() == "1"


@dataclass
class _GenerationCoordinator:
    lock: asyncio.Lock
    active_jobs: int = 0


_GENERATION_COORDINATORS: dict[tuple[int, str], _GenerationCoordinator] = {}


def _coordinator_for(base_url: str) -> _GenerationCoordinator:
    # Tests may create more than one asyncio.run() loop in one Python process.
    # Keeping the lock scoped to the running loop prevents cross-loop lock use.
    key = (id(asyncio.get_running_loop()), base_url)
    coordinator = _GENERATION_COORDINATORS.get(key)
    if coordinator is None:
        coordinator = _GenerationCoordinator(lock=asyncio.Lock())
        _GENERATION_COORDINATORS[key] = coordinator
    return coordinator


async def _begin_generation(base_url: str) -> _GenerationCoordinator:
    coordinator = _coordinator_for(base_url)
    async with coordinator.lock:
        coordinator.active_jobs += 1
    return coordinator


async def _finish_generation(
    base_url: str,
    coordinator: _GenerationCoordinator,
    *,
    submission_attempted: bool,
) -> None:
    """Release only after Aiso's last active job, and only when explicitly opted in."""
    async with coordinator.lock:
        coordinator.active_jobs = max(0, coordinator.active_jobs - 1)
        if (
            submission_attempted
            and coordinator.active_jobs == 0
            and RELEASE_MODELS_AFTER_AISO_JOB
        ):
            await _release_without_masking(base_url)


class GenerationError(RuntimeError):
    """Aiso가 사용자에게 안전하게 표시할 수 있는 이미지 생성 오류."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        kind: Literal["input", "transport", "terminal"] = "terminal",
    ) -> None:
        super().__init__(message)
        # 제출 전 전송 계층 오류만 True다. 제출 시도 뒤 오류는 같은 prompt가 이미
        # 접수됐을 수 있으므로 메시지가 같아도 새 UUID로 재제출하면 안 된다.
        self.retryable = retryable
        # 문자열에 우연히 'seed' 등이 들어가도 제출 후 실패를 입력 오류로 오인하지 않도록
        # 실패 단계를 구조화한다. terminal이 안전한 기본값이다.
        self.kind = kind


def _pre_submission_error_is_retryable(error: comfy_client.ComfyAPIError) -> bool:
    detail = " ".join(str(error).casefold().split())
    return detail in {
        "comfyui에 연결할 수 없습니다.",
        "comfyui 응답 시간이 초과되었습니다.",
    } or (detail.startswith("comfyui가 http 5") and detail.endswith("오류를 반환했습니다."))


GENERATE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "사용자가 등록하고 Agent 사용을 허용한 로컬 ComfyUI 모델 중 요청에 맞는 모델을 "
            "자동 선택해 이미지 한 장을 생성한다. 모델 파일을 다운로드하거나 추천하지 않는다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "선택된 모델에 전달할 구체적인 긍정 프롬프트",
                    "minLength": 1,
                    "maxLength": MAX_PROMPT_LENGTH,
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "피하고 싶은 요소. FLUX.1 split 및 FLUX.2 Klein 기본 템플릿에서는 사용하지 않음",
                    "maxLength": MAX_PROMPT_LENGTH,
                },
                "model_hint": {
                    "type": "string",
                    "description": "사용자가 명시한 등록 모델 이름·ID 또는 특성 태그",
                    "maxLength": 120,
                },
                "width": {"type": "integer", "minimum": 256, "maximum": 2048, "multipleOf": 64},
                "height": {"type": "integer", "minimum": 256, "maximum": 2048, "multipleOf": 64},
                "seed": {
                    "type": "string",
                    "pattern": "^[0-9]{1,20}$",
                    "description": "선택 입력. 64-bit seed를 손실 없이 전달하는 10진 문자열",
                },
            },
            "required": ["prompt"],
        },
    },
}


async def _cancel_without_masking(base_url: str, prompt_id: str) -> bool:
    try:
        result = await comfy_client.cancel_job(base_url, prompt_id)
        return isinstance(result, Mapping) and result.get("cancelled") is True
    except Exception:  # noqa: BLE001 - 원래 timeout/cancel 오류를 보존한다.
        return False


async def _release_without_masking(base_url: str) -> None:
    """제출 뒤 성공·실패·취소 여부와 무관하게 ComfyUI VRAM을 best-effort로 반납한다."""
    try:
        await comfy_client.release_models(base_url)
    except Exception as exc:  # noqa: BLE001 - 원래 생성 결과나 오류를 보존한다.
        logging.warning("ComfyUI 모델 VRAM 해제 요청 실패: %s", exc)


async def _wait_for_terminal_job(base_url: str, prompt_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + GENERATION_TIMEOUT_SECONDS
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            job = await comfy_client.get_job(base_url, prompt_id)
            consecutive_errors = 0
        except comfy_client.ComfyAPIError:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        if job["terminal"]:
            return job
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    cancelled = await _cancel_without_masking(base_url, prompt_id)
    if cancelled:
        raise GenerationError("ComfyUI 이미지 생성 제한 시간 30분을 초과해 해당 작업을 취소했습니다.")
    raise GenerationError(
        "ComfyUI 이미지 생성 제한 시간 30분을 초과했고 해당 작업 취소를 확인하지 못했습니다. "
        "ComfyUI 작업 목록에서 상태를 확인해 주세요."
    )


async def generate_image(
    *,
    base_url: str,
    profiles: Iterable[Mapping[str, Any]],
    prompt: str,
    negative_prompt: str = "",
    model_hint: str = "",
    selected_profile_id: str | None = None,
    selection_context: str = "",
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    seed: int | str | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
) -> dict[str, Any]:
    """모델 선택→노드 검증→제출→poll→output ref 회수의 단일 계약.

    반환값은 ComfyUI output reference만 포함한다. 이미지 바이트나 base64는 포함하지
    않으며, 후속 인증 프록시가 ``jobId``와 저장된 reference로 이미지를 중계한다.
    """
    normalized_url = comfy_client.normalize_base_url(base_url)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > MAX_PROMPT_LENGTH:
        raise GenerationError("프롬프트는 1~4,000자여야 합니다.", kind="input")
    if not isinstance(model_hint, str) or len(model_hint) > 120 or "\x00" in model_hint:
        raise GenerationError("모델 힌트 형식이 올바르지 않습니다.", kind="input")
    if (
        not isinstance(selection_context, str)
        or len(selection_context) > MAX_PROMPT_LENGTH
        or "\x00" in selection_context
    ):
        raise GenerationError("모델 선택 문맥 형식이 올바르지 않습니다.", kind="input")

    try:
        await comfy_client.get_jobs_capability(normalized_url)
        # In automatic mode only profiles explicitly opted into Agent selection
        # are candidates.  A manual exact ID may use an otherwise ready,
        # registered profile whose `agentEnabled` switch is off; it is still
        # subjected to the same asset, workflow, inventory and node checks.
        normalized_profiles = normalize_profiles(
            profiles,
            require_agent_enabled=selected_profile_id is None,
        )
        folders = inventory_folders(normalized_profiles)
        inventory = (
            await comfy_client.get_models_inventory(normalized_url, folders)
            if folders
            else {}
        )
        selected, selection_reason = select_profile(
            normalized_profiles,
            inventory,
            prompt=f"{selection_context}\n{prompt}" if selection_context else prompt,
            model_hint=model_hint,
            selected_profile_id=selected_profile_id,
        )
        prompt_application = apply_prompt_policy(
            selected,
            prompt=prompt,
            negative_prompt=negative_prompt,
        )
        if seed is None:
            resolved_seed: int | str = secrets.randbelow(2**64)
        else:
            resolved_seed = seed
        options = resolve_generation_options(
            selected,
            prompt=prompt_application["effectivePrompt"],
            negative_prompt=prompt_application["effectiveNegativePrompt"],
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
            seed=resolved_seed,
            sampler=sampler,
            scheduler=scheduler,
        )
        node_infos = await comfy_client.get_node_infos(
            normalized_url, node_classes_for_profile(selected)
        )
        validate_runtime_options(selected, options, node_infos)
    except WorkflowValidationError as exc:
        raise GenerationError(str(exc), kind="input") from exc
    except comfy_client.ComfyAPIError as exc:
        raise GenerationError(
            str(exc),
            retryable=_pre_submission_error_is_retryable(exc),
            kind="transport",
        ) from exc

    prompt_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())
    workflow = build_workflow(selected, options, prompt_id=prompt_id)
    workflow_snapshot = snapshot_workflow(
        workflow, allow_user_template=selected.workflow_template is not None
    )
    submission_attempted = False
    coordinator = await _begin_generation(normalized_url)
    try:
        try:
            # 응답이 끊겨도 서버가 prompt를 접수했을 수 있다. UUID 단일 취소는 unknown이면 no-op이므로
            # 요청 직전부터 취소 대상으로 간주해 고아 작업을 남기지 않는다.
            submission_attempted = True
            await comfy_client.submit_prompt(
                normalized_url,
                workflow,
                client_id=client_id,
                prompt_id=prompt_id,
            )
            job = await _wait_for_terminal_job(normalized_url, prompt_id)
        except asyncio.CancelledError:
            cancelled = await _cancel_without_masking(normalized_url, prompt_id)
            if not cancelled:
                logging.error(
                    "ComfyUI 요청 취소 중 작업 %s의 취소 확인을 받지 못했습니다. 작업 목록 확인이 필요합니다.",
                    prompt_id,
                )
            raise
        except GenerationError:
            raise
        except comfy_client.ComfyAPIError as exc:
            cancelled = await _cancel_without_masking(normalized_url, prompt_id)
            cancel_detail = (
                " 해당 작업 취소를 확인했습니다."
                if cancelled
                else " 해당 작업 취소를 확인하지 못했습니다. ComfyUI 작업 목록에서 상태를 확인해 주세요."
            )
            raise GenerationError(f"{exc}{cancel_detail}") from exc

        status = job["status"]
        if status == "failed":
            error_type = (job.get("error") or {}).get("type", "execution_error")
            raise GenerationError(f"ComfyUI 이미지 생성에 실패했습니다 ({error_type}).")
        if status == "cancelled":
            raise GenerationError("ComfyUI 이미지 생성 작업이 취소되었습니다.")
        outputs = job.get("outputs")
        if status != "completed" or not isinstance(outputs, list) or not outputs:
            raise GenerationError("ComfyUI 작업은 끝났지만 결과 이미지를 찾을 수 없습니다.")
        output = outputs[0]
        model_name = primary_model_name(selected)
        summary = f"이미지 생성 완료: {selected.name} ({model_name}), seed {options.seed}"
        if prompt_application["promptPolicy"]["id"] != "none":
            summary += f". {prompt_application['promptPolicy']['label']} 적용"
        uses_negative_prompt = (
            selected.workflow_template is not None
            and bool(selected.workflow_template.bindings["negativePrompt"])
        ) or selected.architecture not in (ARCH_FLUX1_SPLIT, ARCH_FLUX2_KLEIN_4B)
        if not uses_negative_prompt and options.negative_prompt:
            summary += ". 이 모델의 기본 템플릿은 부정 프롬프트를 사용하지 않았습니다"
        ignored_count = (
            0 if selected.workflow_template is not None
            else len(selected.assets) - len(required_assets(selected))
        )
        if ignored_count:
            summary += f". 등록된 추가 자산 {ignored_count}개는 이번 기본 생성에 적용하지 않았습니다"
        image = {
            "jobId": prompt_id,
            "filename": output["filename"],
            "subfolder": output["subfolder"],
            "storageType": output["storageType"],
            "profileId": selected.id,
            "profileName": selected.name,
            "modelName": model_name,
            "selectionReason": selection_reason,
            "prompt": options.prompt,
            "negativePrompt": options.negative_prompt,
            "originalPrompt": prompt_application["originalPrompt"],
            "effectivePrompt": options.prompt,
            "effectiveNegativePrompt": (
                options.negative_prompt if uses_negative_prompt else ""
            ),
            "promptPolicy": prompt_application["promptPolicy"],
            "workflow": workflow_snapshot,
            "seed": str(options.seed),
            "width": options.width,
            "height": options.height,
            "steps": options.steps,
            "cfg": options.cfg,
            "sampler": options.sampler,
            "scheduler": options.scheduler,
            "baseUrl": normalized_url,
        }
        return {"summary": summary, "image": image}
    finally:
        await _finish_generation(
            normalized_url,
            coordinator,
            submission_attempted=submission_attempted,
        )


def result_to_tool_text(result: Mapping[str, Any]) -> str:
    """대용량 이미지 데이터 없이 Agent가 후속 답변에 쓸 수 있는 결과 문장."""
    summary = result.get("summary")
    image = result.get("image")
    if not isinstance(summary, str) or not isinstance(image, Mapping):
        raise GenerationError("이미지 생성 결과 형식이 올바르지 않습니다.")
    fields = ("jobId", "profileName", "modelName", "selectionReason", "seed", "width", "height")
    if any(field not in image for field in fields):
        raise GenerationError("이미지 생성 결과 형식이 올바르지 않습니다.")
    prompt_policy = image.get("promptPolicy")
    policy_line = ""
    if isinstance(prompt_policy, Mapping):
        policy_line = (
            f"프롬프트 정책: {prompt_policy.get('label', '알 수 없음')} "
            f"({prompt_policy.get('id', 'unknown')})\n"
        )
    return (
        f"{summary}\n"
        f"작업 ID: {image['jobId']}\n"
        f"선택 모델: {image['profileName']} / {image['modelName']}\n"
        f"선택 근거: {image['selectionReason']}\n"
        f"{policy_line}"
        f"생성값: seed={image['seed']}, {image['width']}x{image['height']}"
    )


__all__ = [
    "GENERATE_IMAGE_SCHEMA",
    "GENERATION_TIMEOUT_SECONDS",
    "GenerationError",
    "generate_image",
    "result_to_tool_text",
]
