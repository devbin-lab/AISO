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
    refinement_node_classes_for_profile,
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
                    "description": (
                        "피하고 싶은 화질 저하·왜곡·불필요 요소. Aiso 내장 SD 계열은 실제 네거티브 입력에 합치고, "
                        "내장 FLUX 계열은 사용자 스타일과 충돌하지 않는 인식 가능한 항목만 긍정 조건으로 바꾼다. "
                        "사용자 API 워크플로는 등록된 프롬프트 바인딩을 그대로 따른다"
                    ),
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


def _upstream_node_ids(
    workflow: Mapping[str, Mapping[str, Any]],
    output_node_id: Any,
) -> set[str]:
    """표시할 결과 노드에서 실제 입력 연결을 역추적한다."""
    root = str(output_node_id)
    if root not in workflow:
        return set()
    visited: set[str] = set()
    pending = [root]
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        node = workflow.get(node_id)
        if not isinstance(node, Mapping):
            continue
        visited.add(node_id)
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        for value in inputs.values():
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and isinstance(value[1], int)
                and str(value[0]) in workflow
            ):
                pending.append(str(value[0]))
    return visited


def _build_pipeline_snapshot(
    workflow: Mapping[str, Mapping[str, Any]],
    *,
    output_node_id: Any,
    source: Literal["aiso-built-in", "user-workflow"],
    prompt_policy: Mapping[str, Any],
    uses_negative_prompt: bool,
    negative_binding_node_ids: Iterable[str],
    effective_negative_prompt: str,
) -> dict[str, Any] | None:
    """선택된 결과 이미지로 이어지는 노드만 근거로 기능 표시를 만든다."""
    active_node_ids = _upstream_node_ids(workflow, output_node_id)
    if not active_node_ids:
        return None
    def node_sort_key(node_id: str) -> tuple[int, int | str]:
        return (0, int(node_id)) if node_id.isdigit() else (1, node_id)

    active_nodes = [workflow[node_id] for node_id in sorted(active_node_ids, key=node_sort_key)]
    class_types = [str(node.get("class_type") or "") for node in active_nodes]
    processing_nodes = list(dict.fromkeys(
        class_type
        for class_type in class_types
        if any(
            token in class_type.casefold()
            for token in ("upscale", "imagescale", "facerestore", "detailer")
        )
    ))
    added_positive = prompt_policy.get("addedPositive")
    positive_constraints_applied = (
        prompt_policy.get("id") == "flux-positive-constraints-v1"
        and isinstance(added_positive, list)
        and any(isinstance(item, str) and item.strip() for item in added_positive)
    )
    bound_negative_nodes = {str(node_id) for node_id in negative_binding_node_ids}
    negative_reaches_output = (
        uses_negative_prompt
        if source == "aiso-built-in"
        else bool(active_node_ids & bound_negative_nodes)
    )

    return {
        "source": source,
        "nodeCount": len(active_node_ids),
        "vaeDecode": any("vaedecode" in class_type.casefold() for class_type in class_types),
        "negativeMode": (
            "positive-constraints"
            if positive_constraints_applied
            else "conditioning" if negative_reaches_output and bool(effective_negative_prompt.strip())
            else "connected-empty" if negative_reaches_output
            else "not-connected"
        ),
        "scaleProcess": any(
            "upscale" in class_type.casefold() or "imagescale" in class_type.casefold()
            for class_type in class_types
        ),
        "processingNodes": processing_nodes,
    }


def _delivered_dimensions(
    workflow: Mapping[str, Mapping[str, Any]],
    *,
    fallback_width: int,
    fallback_height: int,
    builtin: bool,
) -> tuple[int, int]:
    """Return the dimensions of Aiso's final built-in latent output.

    ``GenerationOptions`` describes the initial latent.  When optional latent
    refinement is active, reporting that initial size as the delivered image
    would be misleading to the user.  Only trust the exact built-in node shape
    assembled by ``build_workflow``; user workflows retain their declared
    option dimensions because their output geometry is arbitrary.

    ``builtin=False`` returns the fallback immediately.  Without that gate this
    function scanned user workflows too and reported **the first** LatentUpscale
    it found, without checking whether that node is even on the output path —
    a graph whose real output is 2048x2048 could be reported as 512x512, and
    that number goes straight into the UI and the agent's answer.

    Widening this to user workflows is deliberately not done: this function only
    understands ``LatentUpscale``, while user graphs commonly use
    ``LatentUpscaleBy`` / ``ImageScale`` / ``ImageUpscaleWithModel``.  A number
    that is only sometimes right is a wrong number.  For user workflows the
    honest signal is the pipeline badge (``scaleProcess`` / ``processingNodes``),
    which names the scaling nodes on the output path instead of guessing a size.
    """
    if not builtin:
        return fallback_width, fallback_height
    for node in workflow.values():
        if not isinstance(node, Mapping) or node.get("class_type") != "LatentUpscale":
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        width = inputs.get("width")
        height = inputs.get("height")
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
            and 1 <= width <= 2_048
            and 1 <= height <= 2_048
        ):
            return width, height
    return fallback_width, fallback_height


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
        mandatory_node_infos = await comfy_client.get_node_infos(
            normalized_url, node_classes_for_profile(selected)
        )
        # The optional quality node is intentionally fetched apart from the
        # mandatory contract.  A normal ComfyUI install without it remains a
        # valid base-generation environment.
        # get_node_infos()가 돌려주는 구체 타입(dict[str, dict])을 그대로 적는다.
        # 소비처(validate_runtime_options·build_workflow)는 Mapping을 받으므로
        # 공변으로 그대로 통과한다.
        optional_node_infos: dict[str, dict[str, Any]] = {}
        optional_classes = refinement_node_classes_for_profile(selected)
        if optional_classes:
            try:
                optional_node_infos = await comfy_client.get_node_infos(
                    normalized_url, optional_classes
                )
            except comfy_client.ComfyAPIError:
                logging.info(
                    "ComfyUI optional latent refinement node is unavailable; using the base workflow."
                )
        node_infos = {**mandatory_node_infos, **optional_node_infos}
        validate_runtime_options(selected, options, mandatory_node_infos)
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
    workflow = build_workflow(selected, options, prompt_id=prompt_id, node_infos=node_infos)
    workflow_snapshot = snapshot_workflow(
        workflow, allow_user_template=selected.workflow_template is not None
    )
    uses_negative_prompt = (
        bool(selected.workflow_template.bindings["negativePrompt"])
        if selected.workflow_template is not None
        else selected.architecture not in (ARCH_FLUX1_SPLIT, ARCH_FLUX2_KLEIN_4B)
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
        pipeline = _build_pipeline_snapshot(
            workflow,
            output_node_id=output.get("nodeId") if isinstance(output, Mapping) else None,
            source="user-workflow" if selected.workflow_template is not None else "aiso-built-in",
            prompt_policy=prompt_application["promptPolicy"],
            uses_negative_prompt=uses_negative_prompt,
            negative_binding_node_ids=(
                (node_id for node_id, _input_name in selected.workflow_template.bindings["negativePrompt"])
                if selected.workflow_template is not None
                else ()
            ),
            effective_negative_prompt=options.negative_prompt,
        )
        negative_reaches_output = bool(
            pipeline and pipeline["negativeMode"] in ("conditioning", "connected-empty")
        )
        delivered_width, delivered_height = _delivered_dimensions(
            workflow,
            fallback_width=options.width,
            fallback_height=options.height,
            builtin=selected.workflow_template is None,
        )
        model_name = primary_model_name(selected)
        summary = f"이미지 생성 완료: {selected.name} ({model_name}), seed {options.seed}"
        if prompt_application["promptPolicy"]["id"] != "none":
            summary += f". 프롬프트 정책: {prompt_application['promptPolicy']['label']}"
        if options.negative_prompt and not negative_reaches_output:
            summary += ". 네거티브 입력이 선택한 결과 이미지 경로에 연결되지 않았습니다"
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
            "originalNegativePrompt": prompt_application["originalNegativePrompt"],
            "effectivePrompt": options.prompt,
            "effectiveNegativePrompt": (
                options.negative_prompt if negative_reaches_output else ""
            ),
            "promptPolicy": prompt_application["promptPolicy"],
            "promptNormalization": {
                "positiveChanged": options.prompt != prompt_application["originalPrompt"],
                "negativeChanged": options.negative_prompt != prompt_application["originalNegativePrompt"],
                "positiveLength": len(options.prompt),
                "negativeLength": len(options.negative_prompt),
                "maxPromptLength": MAX_PROMPT_LENGTH,
            },
            **({"pipeline": pipeline} if pipeline is not None else {}),
            "workflow": workflow_snapshot,
            "seed": str(options.seed),
            "width": delivered_width,
            "height": delivered_height,
            "baseWidth": options.width,
            "baseHeight": options.height,
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
