"""신뢰된 ComfyUI 텍스트→이미지 워크플로와 사용자 모델 프로필 검증.

LLM이나 렌더러가 ``class_type`` 또는 노드 연결을 전달하지 않는다. Aiso가
소유한 템플릿(SD 체크포인트, FLUX.1 split, FLUX.2 Klein 4B)만 API 형식으로 새로 만든다.
모델 파일은 ComfyUI에 있고 이 모듈은 사용자 등록 메타데이터만 다룬다.
"""

from __future__ import annotations

import math
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


CAPABILITY_TEXT_TO_IMAGE = "text-to-image"
ARCH_SD15 = "sd15"
ARCH_SDXL = "sdxl"
ARCH_FLUX1_SPLIT = "flux1-split"
ARCH_FLUX2_KLEIN_4B = "flux2-klein-4b"
ARCH_USER_API = "user-api-workflow"
SUPPORTED_ARCHITECTURES = frozenset(
    {ARCH_SD15, ARCH_SDXL, ARCH_FLUX1_SPLIT, ARCH_FLUX2_KLEIN_4B, ARCH_USER_API}
)

MAX_PROMPT_LENGTH = 4_000
MIN_DIMENSION = 256
MAX_DIMENSION = 2_048
MAX_STEPS = 60
MAX_CFG = 30.0
MAX_SEED = 2**64 - 1
MAX_USER_WORKFLOW_NODES = 96
MAX_USER_WORKFLOW_INPUTS = 64
MAX_USER_WORKFLOW_OUTPUTS = 4
MAX_USER_WORKFLOW_NODE_CLASSES = 64

TRUSTED_SAMPLERS = frozenset({"euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde"})
TRUSTED_SCHEDULERS = frozenset({"normal", "karras", "simple", "beta"})

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_NODE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_NODE_CLASS_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_SAFE_INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_WORKFLOW_BINDING_TARGETS = (
    "positivePrompt", "negativePrompt", "seed", "width", "height", "steps", "cfg",
    "sampler", "scheduler", "filenamePrefix",
)
_MODEL_LOADER_INPUT_NAMES = frozenset(
    {
        "ckpt_name",
        "checkpoint_name",
        "unet_name",
        "model_name",
        "diffusion_model",
        "clip_name",
        "clip_name1",
        "clip_name2",
        "text_encoder_name",
        "vae_name",
        "lora_name",
        "control_net_name",
        "controlnet_name",
        "adapter_name",
    }
)
_MODEL_LOADER_INPUT_RE = re.compile(
    r"(?:^|_)(?:checkpoint|ckpt|unet|model|diffusion|clip|text_encoder|vae|lora|control_?net|adapter)(?:_name|_file|_path)$",
    re.IGNORECASE,
)

_ARCH_ALIASES = {
    "sd15": ARCH_SD15,
    "sd1.5": ARCH_SD15,
    "sd-1.5": ARCH_SD15,
    "sdxl": ARCH_SDXL,
    "sd-xl": ARCH_SDXL,
    "flux1-split": ARCH_FLUX1_SPLIT,
    "flux1_split": ARCH_FLUX1_SPLIT,
    "flux.1-split": ARCH_FLUX1_SPLIT,
    "flux2-klein-4b": ARCH_FLUX2_KLEIN_4B,
    "flux2_klein_4b": ARCH_FLUX2_KLEIN_4B,
    "flux.2-klein-4b": ARCH_FLUX2_KLEIN_4B,
}
_FAMILY_TO_ARCHITECTURE = {
    "sd15": ARCH_SD15,
    "sdxl": ARCH_SDXL,
    "flux1": ARCH_FLUX1_SPLIT,
    "flux2": ARCH_FLUX2_KLEIN_4B,
    "custom": ARCH_USER_API,
}
_CAPABILITY_ALIASES = {
    "text-to-image": CAPABILITY_TEXT_TO_IMAGE,
    "txt2img": CAPABILITY_TEXT_TO_IMAGE,
    "text_to_image": CAPABILITY_TEXT_TO_IMAGE,
}
_ASSET_FOLDERS = {
    "checkpoint": "checkpoints",
    "diffusion_model": "diffusion_models",
    "text_encoder": "text_encoders",
    "vae": "vae",
    "lora": "loras",
    "controlnet": "controlnet",
}
_INVENTORY_MODEL_FOLDERS = frozenset(_ASSET_FOLDERS.values())


class WorkflowValidationError(ValueError):
    """프로필, 생성 입력 또는 ComfyUI 노드 계약이 안전 기준을 벗어남."""


@dataclass(frozen=True)
class ModelAsset:
    id: str
    kind: str
    slot: str | None
    file_name: str
    comfy_name: str
    relative_path: str
    size: int
    sha256: str

    @property
    def folder(self) -> str:
        return _ASSET_FOLDERS.get(self.kind, self.relative_path.split("/", 1)[0])


@dataclass(frozen=True)
class WorkflowAssetBinding:
    node_id: str
    input_name: str
    asset_id: str
    sha256: str
    relative_path: str
    comfy_name: str


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    source_file_name: str
    sha256: str
    graph: dict[str, dict[str, Any]]
    bindings: dict[str, tuple[tuple[str, str], ...]]
    asset_bindings: tuple[WorkflowAssetBinding, ...]


@dataclass(frozen=True)
class ModelProfile:
    id: str
    name: str
    architecture: str
    agent_enabled: bool
    capabilities: frozenset[str]
    tags: tuple[str, ...]
    priority: int
    assets: tuple[ModelAsset, ...]
    workflow_template_id: str
    workflow_template: WorkflowTemplate | None
    defaults: "ProfileDefaults"


@dataclass(frozen=True)
class ProfileDefaults:
    width: int
    height: int
    steps: int
    cfg: float
    sampler: str | None
    scheduler: str | None


@dataclass(frozen=True)
class GenerationOptions:
    prompt: str
    negative_prompt: str
    width: int
    height: int
    steps: int
    cfg: float
    seed: int
    sampler: str
    scheduler: str


@dataclass(frozen=True)
class NodeContract:
    module: str
    required_inputs: frozenset[str]


SD_NODE_CONTRACTS: dict[str, NodeContract] = {
    "CheckpointLoaderSimple": NodeContract("nodes", frozenset({"ckpt_name"})),
    "CLIPTextEncode": NodeContract("nodes", frozenset({"text", "clip"})),
    "EmptyLatentImage": NodeContract("nodes", frozenset({"width", "height", "batch_size"})),
    "KSampler": NodeContract(
        "nodes",
        frozenset(
            {
                "model",
                "seed",
                "steps",
                "cfg",
                "sampler_name",
                "scheduler",
                "positive",
                "negative",
                "latent_image",
                "denoise",
            }
        ),
    ),
    "VAEDecode": NodeContract("nodes", frozenset({"samples", "vae"})),
    "SaveImage": NodeContract("nodes", frozenset({"images", "filename_prefix"})),
}

FLUX_NODE_CONTRACTS: dict[str, NodeContract] = {
    "UNETLoader": NodeContract("nodes", frozenset({"unet_name", "weight_dtype"})),
    "DualCLIPLoader": NodeContract("nodes", frozenset({"clip_name1", "clip_name2", "type"})),
    "VAELoader": NodeContract("nodes", frozenset({"vae_name"})),
    "CLIPTextEncode": NodeContract("nodes", frozenset({"text", "clip"})),
    "EmptySD3LatentImage": NodeContract(
        "comfy_extras.nodes_sd3", frozenset({"width", "height", "batch_size"})
    ),
    "FluxGuidance": NodeContract("comfy_extras.nodes_flux", frozenset({"conditioning", "guidance"})),
    "BasicGuider": NodeContract(
        "comfy_extras.nodes_custom_sampler", frozenset({"model", "conditioning"})
    ),
    "KSamplerSelect": NodeContract(
        "comfy_extras.nodes_custom_sampler", frozenset({"sampler_name"})
    ),
    "BasicScheduler": NodeContract(
        "comfy_extras.nodes_custom_sampler", frozenset({"model", "scheduler", "steps", "denoise"})
    ),
    "RandomNoise": NodeContract("comfy_extras.nodes_custom_sampler", frozenset({"noise_seed"})),
    "SamplerCustomAdvanced": NodeContract(
        "comfy_extras.nodes_custom_sampler",
        frozenset({"noise", "guider", "sampler", "sigmas", "latent_image"}),
    ),
    "VAEDecode": NodeContract("nodes", frozenset({"samples", "vae"})),
    "SaveImage": NodeContract("nodes", frozenset({"images", "filename_prefix"})),
}

# ComfyUI 공식 FLUX.2 [klein] 4B text-to-image 템플릿의 API 그래프 계약.
# 노드 클래스와 필수 입력을 모두 런타임 object_info로 대조해, 같은 이름의
# 커스텀 노드나 지원하지 않는 ComfyUI 버전에는 workflow를 제출하지 않는다.
FLUX2_KLEIN_NODE_CONTRACTS: dict[str, NodeContract] = {
    "UNETLoader": NodeContract("nodes", frozenset({"unet_name", "weight_dtype"})),
    "CLIPLoader": NodeContract("nodes", frozenset({"clip_name", "type"})),
    "VAELoader": NodeContract("nodes", frozenset({"vae_name"})),
    "CLIPTextEncode": NodeContract("nodes", frozenset({"text", "clip"})),
    "ConditioningZeroOut": NodeContract("nodes", frozenset({"conditioning"})),
    "CFGGuider": NodeContract(
        "comfy_extras.nodes_custom_sampler", frozenset({"model", "positive", "negative", "cfg"})
    ),
    "KSamplerSelect": NodeContract(
        "comfy_extras.nodes_custom_sampler", frozenset({"sampler_name"})
    ),
    "Flux2Scheduler": NodeContract(
        "comfy_extras.nodes_flux", frozenset({"steps", "width", "height"})
    ),
    "RandomNoise": NodeContract("comfy_extras.nodes_custom_sampler", frozenset({"noise_seed"})),
    "EmptyFlux2LatentImage": NodeContract(
        "comfy_extras.nodes_flux", frozenset({"width", "height", "batch_size"})
    ),
    "SamplerCustomAdvanced": NodeContract(
        "comfy_extras.nodes_custom_sampler",
        frozenset({"noise", "guider", "sampler", "sigmas", "latent_image"}),
    ),
    "VAEDecode": NodeContract("nodes", frozenset({"samples", "vae"})),
    "SaveImage": NodeContract("nodes", frozenset({"images", "filename_prefix"})),
}


def node_contracts_for_architecture(architecture: str) -> dict[str, NodeContract]:
    if architecture in (ARCH_SD15, ARCH_SDXL):
        return SD_NODE_CONTRACTS
    if architecture == ARCH_FLUX1_SPLIT:
        return FLUX_NODE_CONTRACTS
    if architecture == ARCH_FLUX2_KLEIN_4B:
        return FLUX2_KLEIN_NODE_CONTRACTS
    raise WorkflowValidationError("지원하지 않는 모델 아키텍처입니다.")


def node_classes_for_profile(profile: ModelProfile) -> frozenset[str]:
    if profile.workflow_template is not None:
        return frozenset(node["class_type"] for node in profile.workflow_template.graph.values())
    return frozenset(node_contracts_for_architecture(profile.architecture))


def _required_text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise WorkflowValidationError(f"{field} 형식이 올바르지 않습니다.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or "\x00" in normalized:
        raise WorkflowValidationError(f"{field} 형식이 올바르지 않습니다.")
    return normalized


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise WorkflowValidationError(f"{field} 형식이 올바르지 않습니다.")
    return value


def _safe_relative_name(value: Any, field: str, *, basename_only: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise WorkflowValidationError(f"{field} 형식이 올바르지 않습니다.")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise WorkflowValidationError(f"{field} 형식이 올바르지 않습니다.")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts) or (basename_only and len(parts) != 1):
        raise WorkflowValidationError(f"{field} 형식이 올바르지 않습니다.")
    if not normalized.casefold().endswith(".safetensors"):
        raise WorkflowValidationError("Agent 모델 구성요소는 .safetensors 파일만 사용할 수 있습니다.")
    return normalized


def _normalize_slot(kind: str, value: Any) -> str:
    slot = _required_text(value, "모델 구성요소 slot", max_length=64).casefold().replace("-", "_")
    aliases = {
        "checkpoint": "checkpoint",
        "base": {
            "checkpoint": "checkpoint",
            "diffusion_model": "diffusion_model",
            "vae": "vae",
        }.get(kind, "base"),
        "model": "diffusion_model" if kind == "diffusion_model" else "model",
        "diffusion_model": "diffusion_model",
        "vae": "vae",
        "lora": "lora",
        "controlnet": "controlnet",
        "clip_l": "clip_l",
        "clipl": "clip_l",
        "t5_xxl": "t5xxl",
        "t5xxl": "t5xxl",
        "qwen3": "qwen3",
        "qwen_3": "qwen3",
        "qwen_3_4b": "qwen3",
    }
    normalized = aliases.get(slot, slot)
    if kind == "text_encoder" and normalized not in {"clip_l", "t5xxl", "qwen3"}:
        raise WorkflowValidationError("텍스트 인코더 slot은 CLIP-L, T5XXL 또는 Qwen 3이어야 합니다.")
    expected = {
        "checkpoint": "checkpoint",
        "diffusion_model": "diffusion_model",
        "vae": "vae",
        "lora": "lora",
        "controlnet": "controlnet",
    }.get(kind)
    if kind != "text_encoder" and normalized != expected:
        raise WorkflowValidationError(f"{kind} 구성요소 slot이 올바르지 않습니다.")
    return normalized


def _normalize_profile_defaults(
    raw: Any, architecture: str, *, allow_user_options: bool = False
) -> ProfileDefaults:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("모델 생성 기본값 형식이 올바르지 않습니다.")
    fallback_dimension = 512 if architecture == ARCH_SD15 else 1024
    fallback_cfg = 3.5 if architecture == ARCH_FLUX1_SPLIT else 1.0 if architecture == ARCH_FLUX2_KLEIN_4B else 7.0
    width = _bounded_int(raw.get("width", fallback_dimension), "기본 너비", MIN_DIMENSION, MAX_DIMENSION)
    height = _bounded_int(raw.get("height", fallback_dimension), "기본 높이", MIN_DIMENSION, MAX_DIMENSION)
    if width % 64 or height % 64:
        raise WorkflowValidationError("기본 이미지 너비와 높이는 64의 배수여야 합니다.")
    fallback_steps = 4 if architecture == ARCH_FLUX2_KLEIN_4B else 20
    steps = _bounded_int(raw.get("steps", fallback_steps), "기본 steps", 1, MAX_STEPS)
    cfg = _bounded_float(raw.get("cfg", fallback_cfg), "기본 CFG", 0.0, MAX_CFG)
    sampler = raw.get("sampler")
    scheduler = raw.get("scheduler")
    if sampler is not None and (
        not isinstance(sampler, str)
        or not 1 <= len(sampler) <= 80
        or "\x00" in sampler
        or (not allow_user_options and sampler not in TRUSTED_SAMPLERS)
    ):
        raise WorkflowValidationError("모델 기본 sampler가 지원 범위를 벗어났습니다.")
    if scheduler is not None and (
        not isinstance(scheduler, str)
        or not 1 <= len(scheduler) <= 80
        or "\x00" in scheduler
        or (not allow_user_options and scheduler not in TRUSTED_SCHEDULERS)
    ):
        raise WorkflowValidationError("모델 기본 scheduler가 지원 범위를 벗어났습니다.")
    return ProfileDefaults(width, height, steps, cfg, sampler, scheduler)


def _normalize_workflow_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise WorkflowValidationError("사용자 워크플로 입력 구조가 너무 깊습니다.")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowValidationError("사용자 워크플로 숫자 입력이 올바르지 않습니다.")
        return value
    if isinstance(value, str):
        if len(value) > 16_384 or "\x00" in value:
            raise WorkflowValidationError("사용자 워크플로 문자열 입력이 허용 범위를 벗어났습니다.")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > 256:
            raise WorkflowValidationError("사용자 워크플로 배열 입력이 너무 큽니다.")
        return [_normalize_workflow_value(item, depth=depth + 1) for item in value]
    raise WorkflowValidationError("사용자 워크플로 입력 형식이 올바르지 않습니다.")


def _workflow_content_hash(
    graph: Mapping[str, Any],
    bindings: Mapping[str, Any],
    asset_bindings: Sequence[WorkflowAssetBinding] | None = None,
) -> str:
    payload: dict[str, Any] = {"graph": graph, "bindings": bindings}
    if asset_bindings is not None:
        payload["assetBindings"] = [
            {
                "nodeId": binding.node_id,
                "input": binding.input_name,
                "assetId": binding.asset_id,
                "sha256": binding.sha256,
                "relativePath": binding.relative_path,
                "comfyName": binding.comfy_name,
            }
            for binding in asset_bindings
        ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_model_loader_input(input_name: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.casefold().endswith(".safetensors"):
        return False
    return _is_model_loader_input_name(input_name)


def _is_model_loader_input_name(input_name: str) -> bool:
    return input_name.casefold() in _MODEL_LOADER_INPUT_NAMES or bool(
        _MODEL_LOADER_INPUT_RE.fullmatch(input_name)
    )


def _workflow_loader_refs(graph: Mapping[str, Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (node_id, input_name)
        for node_id, node in graph.items()
        for input_name, value in node["inputs"].items()
        if _is_model_loader_input(input_name, value)
    )


def _workflow_binding_key(node_id: str, input_name: str) -> tuple[str, str]:
    return node_id, input_name


def _normalized_comfy_asset_name(value: str) -> str:
    return value.replace("\\", "/").removeprefix("./").casefold()


def _validate_user_workflow_resource_policy(graph: Mapping[str, Mapping[str, Any]]) -> None:
    if len(graph) > MAX_USER_WORKFLOW_NODES:
        raise WorkflowValidationError(
            f"사용자 워크플로 노드는 최대 {MAX_USER_WORKFLOW_NODES}개까지 허용됩니다."
        )
    save_images = 0
    classes: set[str] = set()
    for node in graph.values():
        classes.add(node["class_type"])
        if node["class_type"] == "SaveImage":
            save_images += 1
        inputs = node["inputs"]
        if len(inputs) > MAX_USER_WORKFLOW_INPUTS:
            raise WorkflowValidationError(
                f"사용자 워크플로 노드 입력은 최대 {MAX_USER_WORKFLOW_INPUTS}개까지 허용됩니다."
            )
        for input_name, value in inputs.items():
            if input_name in {"width", "height", "steps", "cfg", "guidance", "batch_size"} and isinstance(value, list):
                raise WorkflowValidationError(
                    f"Agent 사용자 워크플로의 {input_name}은 제한을 적용할 수 있는 직접 값이어야 합니다."
                )
            if _is_model_loader_input_name(input_name) and (
                not isinstance(value, str) or not value.casefold().endswith(".safetensors")
            ):
                raise WorkflowValidationError(
                    f"Agent 사용자 워크플로의 {input_name}은 등록 가능한 SafeTensors 모델 값이어야 합니다."
                )
            if input_name == "batch_size" and value != 1:
                raise WorkflowValidationError("Agent 사용자 워크플로의 batch_size는 1이어야 합니다.")
            if input_name in {"width", "height"} and isinstance(value, (int, float)) and not isinstance(value, bool):
                if not isinstance(value, int) or not MIN_DIMENSION <= value <= MAX_DIMENSION or value % 64:
                    raise WorkflowValidationError(
                        f"Agent 사용자 워크플로의 {input_name}은 256~2048 범위의 64 배수여야 합니다."
                    )
            if input_name == "steps" and isinstance(value, (int, float)) and not isinstance(value, bool):
                if not isinstance(value, int) or not 1 <= value <= MAX_STEPS:
                    raise WorkflowValidationError("Agent 사용자 워크플로의 steps는 1~60 범위여야 합니다.")
            if input_name in {"cfg", "guidance"} and isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(float(value)) or not 0 <= float(value) <= MAX_CFG:
                    raise WorkflowValidationError(
                        f"Agent 사용자 워크플로의 {input_name}은 0~30 범위여야 합니다."
                    )
    if save_images == 0 or save_images > MAX_USER_WORKFLOW_OUTPUTS:
        raise WorkflowValidationError(
            f"Agent 사용자 워크플로의 SaveImage 출력은 1~{MAX_USER_WORKFLOW_OUTPUTS}개여야 합니다."
        )
    if len(classes) > MAX_USER_WORKFLOW_NODE_CLASSES:
        raise WorkflowValidationError(
            f"Agent 사용자 워크플로의 서로 다른 노드 종류는 최대 {MAX_USER_WORKFLOW_NODE_CLASSES}개까지 허용됩니다."
        )


def _normalize_workflow_template(raw: Any) -> WorkflowTemplate:
    if not isinstance(raw, Mapping) or raw.get("schemaVersion") != 1:
        raise WorkflowValidationError("사용자 워크플로 템플릿 형식이 올바르지 않습니다.")
    graph_raw = raw.get("graph")
    if not isinstance(graph_raw, Mapping) or not 1 <= len(graph_raw) <= MAX_USER_WORKFLOW_NODES:
        raise WorkflowValidationError("사용자 워크플로 노드 수가 올바르지 않습니다.")
    graph: dict[str, dict[str, Any]] = {}
    for node_id, node_raw in graph_raw.items():
        if not isinstance(node_id, str) or not _SAFE_NODE_ID_RE.fullmatch(node_id):
            raise WorkflowValidationError("사용자 워크플로 노드 ID가 올바르지 않습니다.")
        if not isinstance(node_raw, Mapping) or set(node_raw) != {"class_type", "inputs"}:
            raise WorkflowValidationError("사용자 워크플로 노드 형식이 올바르지 않습니다.")
        class_type = node_raw.get("class_type")
        inputs_raw = node_raw.get("inputs")
        if (
            not isinstance(class_type, str)
            or not _SAFE_NODE_CLASS_RE.fullmatch(class_type)
            or not isinstance(inputs_raw, Mapping)
            or len(inputs_raw) > MAX_USER_WORKFLOW_INPUTS
        ):
            raise WorkflowValidationError("사용자 워크플로 노드 계약이 올바르지 않습니다.")
        inputs: dict[str, Any] = {}
        for input_name, value in inputs_raw.items():
            if not isinstance(input_name, str) or not _SAFE_INPUT_NAME_RE.fullmatch(input_name):
                raise WorkflowValidationError("사용자 워크플로 입력 이름이 올바르지 않습니다.")
            inputs[input_name] = _normalize_workflow_value(value)
        graph[node_id] = {"class_type": class_type, "inputs": inputs}
    _validate_user_workflow_resource_policy(graph)

    bindings_raw = raw.get("bindings")
    if not isinstance(bindings_raw, Mapping) or set(bindings_raw) != set(_WORKFLOW_BINDING_TARGETS):
        raise WorkflowValidationError("사용자 워크플로 입력 바인딩 형식이 올바르지 않습니다.")
    bindings: dict[str, tuple[tuple[str, str], ...]] = {}
    hash_bindings: dict[str, list[dict[str, str]]] = {}
    for target in _WORKFLOW_BINDING_TARGETS:
        refs_raw = bindings_raw.get(target)
        if not isinstance(refs_raw, Sequence) or isinstance(refs_raw, (str, bytes)) or len(refs_raw) > 256:
            raise WorkflowValidationError("사용자 워크플로 입력 바인딩 형식이 올바르지 않습니다.")
        refs: list[tuple[str, str]] = []
        hash_refs: list[dict[str, str]] = []
        for ref in refs_raw:
            if not isinstance(ref, Mapping) or set(ref) != {"nodeId", "input"}:
                raise WorkflowValidationError("사용자 워크플로 입력 바인딩 형식이 올바르지 않습니다.")
            node_id = ref.get("nodeId")
            input_name = ref.get("input")
            if (
                not isinstance(node_id, str)
                or not isinstance(input_name, str)
                or node_id not in graph
                or input_name not in graph[node_id]["inputs"]
            ):
                raise WorkflowValidationError("사용자 워크플로 입력 바인딩 대상이 올바르지 않습니다.")
            pair = (node_id, input_name)
            if pair not in refs:
                refs.append(pair)
                hash_refs.append({"nodeId": node_id, "input": input_name})
        bindings[target] = tuple(refs)
        hash_bindings[target] = hash_refs
    if not bindings["positivePrompt"] or not bindings["filenamePrefix"]:
        raise WorkflowValidationError("사용자 워크플로의 프롬프트 또는 출력 바인딩이 없습니다.")
    for node_id, input_name in bindings["filenamePrefix"]:
        if graph[node_id]["class_type"] != "SaveImage" or input_name != "filename_prefix":
            raise WorkflowValidationError("사용자 워크플로 출력 바인딩이 안전하지 않습니다.")
    expected_outputs = {
        (node_id, "filename_prefix")
        for node_id, node in graph.items()
        if node["class_type"] == "SaveImage"
    }
    if set(bindings["filenamePrefix"]) != expected_outputs:
        raise WorkflowValidationError("모든 SaveImage 출력 경로가 Aiso에 바인딩되어야 합니다.")

    loader_refs = set(_workflow_loader_refs(graph))
    asset_bindings_raw = raw.get("assetBindings")
    is_legacy = asset_bindings_raw is None and "assetBindings" not in raw
    if asset_bindings_raw is None and not is_legacy:
        raise WorkflowValidationError("사용자 워크플로 모델 연결 계약이 올바르지 않습니다.")
    if is_legacy:
        asset_bindings: tuple[WorkflowAssetBinding, ...] = ()
    else:
        if (
            not isinstance(asset_bindings_raw, Sequence)
            or isinstance(asset_bindings_raw, (str, bytes))
            or len(asset_bindings_raw) > MAX_USER_WORKFLOW_NODES
        ):
            raise WorkflowValidationError("사용자 워크플로 모델 연결 계약이 올바르지 않습니다.")
        parsed_asset_bindings: list[WorkflowAssetBinding] = []
        seen_loader_refs: set[tuple[str, str]] = set()
        for raw_binding in asset_bindings_raw:
            if not isinstance(raw_binding, Mapping) or set(raw_binding) != {
                "nodeId", "input", "assetId", "sha256", "relativePath", "comfyName"
            }:
                raise WorkflowValidationError("사용자 워크플로 모델 연결 계약이 올바르지 않습니다.")
            node_id = raw_binding.get("nodeId")
            input_name = raw_binding.get("input")
            asset_id = raw_binding.get("assetId")
            sha256_value = raw_binding.get("sha256")
            relative_path = raw_binding.get("relativePath")
            comfy_name = raw_binding.get("comfyName")
            ref = (node_id, input_name)
            if (
                not isinstance(node_id, str)
                or not isinstance(input_name, str)
                or ref not in loader_refs
                or ref in seen_loader_refs
                or not isinstance(asset_id, str)
                or not _ID_RE.fullmatch(asset_id)
            ):
                raise WorkflowValidationError("사용자 워크플로 모델 연결 계약이 올바르지 않습니다.")
            if (
                not isinstance(sha256_value, str)
                or not _SHA256_RE.fullmatch(sha256_value)
                or not isinstance(relative_path, str)
                or not isinstance(comfy_name, str)
                or "\\" in relative_path
                or "\\" in comfy_name
                or _safe_relative_name(relative_path, "모델 상대 경로") != relative_path.replace("\\", "/")
                or _safe_relative_name(comfy_name, "ComfyUI 모델명") != comfy_name.replace("\\", "/")
                or graph[node_id]["inputs"][input_name] != comfy_name
            ):
                raise WorkflowValidationError("사용자 워크플로 모델 연결 계약이 올바르지 않습니다.")
            seen_loader_refs.add(ref)
            parsed_asset_bindings.append(
                WorkflowAssetBinding(
                    node_id=node_id,
                    input_name=input_name,
                    asset_id=asset_id,
                    sha256=sha256_value.casefold(),
                    relative_path=relative_path.replace("\\", "/"),
                    comfy_name=comfy_name.replace("\\", "/"),
                )
            )
        asset_bindings = tuple(parsed_asset_bindings)

    sha256 = _workflow_content_hash(
        graph, hash_bindings, None if is_legacy else asset_bindings
    )
    template_id = f"user.{sha256[:20]}.txt2img.v1"
    source_file_name = raw.get("sourceFileName")
    if (
        not isinstance(source_file_name, str)
        or not source_file_name
        or len(source_file_name) > 200
        or "/" in source_file_name
        or "\\" in source_file_name
        or "\x00" in source_file_name
    ):
        raise WorkflowValidationError("사용자 워크플로 파일 이름이 올바르지 않습니다.")
    if raw.get("sha256") != sha256 or raw.get("id") != template_id:
        raise WorkflowValidationError("사용자 워크플로 내용 해시가 일치하지 않습니다.")
    return WorkflowTemplate(template_id, source_file_name, sha256, graph, bindings, asset_bindings)


def normalize_asset(raw: Mapping[str, Any]) -> ModelAsset:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("모델 구성요소 형식이 올바르지 않습니다.")
    asset_id = _safe_id(raw.get("id"), "모델 구성요소 ID")
    kind = raw.get("kind")
    if kind not in {*_ASSET_FOLDERS, "custom"}:
        raise WorkflowValidationError("지원하지 않는 모델 구성요소 종류입니다.")
    slot = None if kind == "custom" else _normalize_slot(kind, raw.get("slot"))
    if kind == "custom" and raw.get("slot") is not None:
        raise WorkflowValidationError("직접 연결 파일에는 자동 모델 slot을 지정할 수 없습니다.")
    file_name = _safe_relative_name(raw.get("fileName"), "모델 파일명", basename_only=True)
    comfy_name = _safe_relative_name(raw.get("comfyName"), "ComfyUI 모델명")
    relative_path = _safe_relative_name(raw.get("relativePath"), "모델 상대 경로")
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 2**50:
        raise WorkflowValidationError("모델 파일 크기가 올바르지 않습니다.")
    sha256 = raw.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise WorkflowValidationError("모델 SHA-256 형식이 올바르지 않습니다.")
    return ModelAsset(
        id=asset_id,
        kind=kind,
        slot=slot,
        file_name=file_name,
        comfy_name=comfy_name,
        relative_path=relative_path,
        size=size,
        sha256=sha256.lower(),
    )


def normalize_profile(
    raw: Mapping[str, Any],
    *,
    require_agent_enabled: bool = True,
) -> ModelProfile:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("모델 프로필 형식이 올바르지 않습니다.")
    profile_id = _safe_id(raw.get("id"), "모델 프로필 ID")
    name_raw = raw.get("name", raw.get("displayName"))
    name = _required_text(name_raw, "모델 프로필 이름", max_length=120)
    family_raw = raw.get("family")
    architecture_raw = raw.get("architecture")
    workflow_template = (
        _normalize_workflow_template(raw.get("workflowTemplate"))
        if raw.get("workflowTemplate") is not None
        else None
    )
    if isinstance(family_raw, str):
        architecture = _FAMILY_TO_ARCHITECTURE.get(family_raw.strip().casefold())
        if isinstance(architecture_raw, str):
            legacy = _ARCH_ALIASES.get(architecture_raw.strip().casefold())
            if legacy is not None and legacy != architecture:
                raise WorkflowValidationError("모델 family와 architecture가 서로 다릅니다.")
    elif isinstance(architecture_raw, str):
        architecture = _ARCH_ALIASES.get(architecture_raw.strip().casefold())
    else:
        architecture = None
    if architecture == ARCH_USER_API and workflow_template is None:
        raise WorkflowValidationError("사용자 모델에 Agent용 API 워크플로가 연결되지 않았습니다.")
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise WorkflowValidationError("지원하지 않는 모델 아키텍처입니다.")
    agent_enabled = raw.get("agentEnabled")
    if not isinstance(agent_enabled, bool):
        raise WorkflowValidationError("Agent 자동 선택 설정 형식이 올바르지 않습니다.")
    if require_agent_enabled and agent_enabled is not True:
        raise WorkflowValidationError("Agent 자동 선택이 허용되지 않은 모델입니다.")

    capabilities_raw = raw.get("capabilities")
    if not isinstance(capabilities_raw, Sequence) or isinstance(capabilities_raw, (str, bytes)):
        raise WorkflowValidationError("모델 capability 형식이 올바르지 않습니다.")
    capabilities: set[str] = set()
    for capability in capabilities_raw:
        if not isinstance(capability, str):
            raise WorkflowValidationError("모델 capability 형식이 올바르지 않습니다.")
        normalized_capability = _CAPABILITY_ALIASES.get(capability.strip().casefold())
        if normalized_capability:
            capabilities.add(normalized_capability)
    if CAPABILITY_TEXT_TO_IMAGE not in capabilities:
        raise WorkflowValidationError("텍스트 이미지 생성을 지원하지 않는 모델입니다.")

    tags_raw = raw.get("tags", [])
    if not isinstance(tags_raw, Sequence) or isinstance(tags_raw, (str, bytes)) or len(tags_raw) > 32:
        raise WorkflowValidationError("모델 태그 형식이 올바르지 않습니다.")
    tags: list[str] = []
    for tag in tags_raw:
        normalized_tag = _required_text(tag, "모델 태그", max_length=64).casefold()
        if normalized_tag not in tags:
            tags.append(normalized_tag)

    priority = raw.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool) or not -100 <= priority <= 100:
        raise WorkflowValidationError("모델 우선순위가 올바르지 않습니다.")
    assets_raw = raw.get("assets")
    if not isinstance(assets_raw, Sequence) or isinstance(assets_raw, (str, bytes)):
        raise WorkflowValidationError("모델 구성요소 목록 형식이 올바르지 않습니다.")
    assets = tuple(normalize_asset(asset) for asset in assets_raw)
    if len({asset.id for asset in assets}) != len(assets):
        raise WorkflowValidationError("모델 구성요소 ID가 중복되었습니다.")

    family_for_template = {
        ARCH_SD15: "sd15",
        ARCH_SDXL: "sdxl",
        ARCH_FLUX1_SPLIT: "flux1",
        ARCH_FLUX2_KLEIN_4B: "flux2",
        ARCH_USER_API: "custom",
    }[architecture]
    expected_template = f"{family_for_template}.txt2img.v1"
    workflow_template_id = raw.get("workflowTemplateId", expected_template)
    if workflow_template is not None:
        expected_template = workflow_template.id
    if workflow_template_id != expected_template:
        raise WorkflowValidationError("Agent가 신뢰하지 않는 워크플로 템플릿 ID입니다.")
    defaults = _normalize_profile_defaults(
        raw.get("defaults"), architecture, allow_user_options=workflow_template is not None
    )

    profile = ModelProfile(
        id=profile_id,
        name=name,
        architecture=architecture,
        agent_enabled=agent_enabled,
        capabilities=frozenset(capabilities),
        tags=tuple(tags),
        priority=priority,
        assets=assets,
        workflow_template_id=workflow_template_id,
        workflow_template=workflow_template,
        defaults=defaults,
    )
    required_assets(profile)  # 빠진 slot과 현재 템플릿이 무시할 구성요소를 함께 거부한다.
    return profile


def user_workflow_assets(profile: ModelProfile) -> tuple[tuple[WorkflowAssetBinding, ModelAsset], ...]:
    template = profile.workflow_template
    if template is None:
        return ()
    expected = _workflow_loader_refs(template.graph)
    if not expected:
        raise WorkflowValidationError(
            "사용자 워크플로에서 등록 모델과 연결할 SafeTensors 로더를 찾지 못했습니다."
        )
    by_ref: dict[tuple[str, str], WorkflowAssetBinding] = {}
    for binding in template.asset_bindings:
        key = _workflow_binding_key(binding.node_id, binding.input_name)
        if key in by_ref:
            raise WorkflowValidationError("사용자 워크플로 모델 연결이 중복되었습니다.")
        by_ref[key] = binding
    by_id = {asset.id: asset for asset in profile.assets}
    resolved: list[tuple[WorkflowAssetBinding, ModelAsset]] = []
    for node_id, input_name in expected:
        binding = by_ref.get((node_id, input_name))
        literal = template.graph[node_id]["inputs"][input_name]
        asset = by_id.get(binding.asset_id) if binding else None
        if (
            binding is None
            or not isinstance(literal, str)
            or binding.comfy_name != literal
            or asset is None
            or asset.sha256 != binding.sha256
            or asset.relative_path != binding.relative_path
            or _normalized_comfy_asset_name(binding.comfy_name) not in {
                _normalized_comfy_asset_name(asset.comfy_name),
                _normalized_comfy_asset_name(asset.relative_path),
            }
        ):
            raise WorkflowValidationError(
                f"사용자 워크플로 모델 로더가 등록 자산과 연결되지 않았습니다: {node_id}.{input_name}"
            )
        resolved.append((binding, asset))
    if set(by_ref) != set(expected):
        raise WorkflowValidationError("사용자 워크플로 모델 연결 대상이 올바르지 않습니다.")
    return tuple(resolved)


def required_assets(profile: ModelProfile) -> dict[str, ModelAsset]:
    if profile.workflow_template is not None:
        # User templates are not slot-driven.  Still validate their explicit
        # loader-to-asset contract before allowing profile normalization.
        user_workflow_assets(profile)
        return {}
    if profile.architecture in (ARCH_SD15, ARCH_SDXL):
        expected = {"checkpoint"}
    elif profile.architecture == ARCH_FLUX1_SPLIT:
        expected = {
            "diffusion_model",
            "clip_l",
            "t5xxl",
            "vae",
        }
    elif profile.architecture == ARCH_FLUX2_KLEIN_4B:
        expected = {
            "diffusion_model",
            "qwen3",
            "vae",
        }
    else:
        raise WorkflowValidationError("지원하지 않는 모델 아키텍처입니다.")
    keyed: dict[str, ModelAsset] = {}
    for asset in profile.assets:
        if asset.slot not in expected:
            # 레지스트리는 수동 ComfyUI 사용을 위한 추가 자산도 보관할 수 있다.
            # 신뢰 템플릿은 해당 아키텍처의 명시된 필수 슬롯만 소비한다.
            continue
        key = asset.slot
        if key in keyed:
            raise WorkflowValidationError("같은 역할의 모델 구성요소가 중복되었습니다.")
        keyed[key] = asset
    if set(keyed) != expected:
        raise WorkflowValidationError("모델 아키텍처에 필요한 .safetensors 구성요소가 완전하지 않습니다.")
    return keyed


def normalize_profiles(
    profiles: Iterable[Mapping[str, Any]],
    *,
    require_agent_enabled: bool = True,
) -> list[ModelProfile]:
    if isinstance(profiles, (str, bytes, Mapping)):
        raise WorkflowValidationError("모델 프로필 목록 형식이 올바르지 않습니다.")
    normalized: list[ModelProfile] = []
    for raw in profiles:
        try:
            normalized.append(normalize_profile(raw, require_agent_enabled=require_agent_enabled))
        except WorkflowValidationError:
            # A malformed/incomplete profile must not make every other registered
            # candidate unusable. In automatic mode, auto-disabled entries are
            # intentionally skipped here as well.
            continue
    if not normalized:
        raise WorkflowValidationError("Agent가 사용할 수 있는 완전한 모델 프로필이 없습니다.")
    if len({profile.id for profile in normalized}) != len(normalized):
        raise WorkflowValidationError("모델 프로필 ID가 중복되었습니다.")
    return normalized


def inventory_folders(profiles: Iterable[ModelProfile]) -> frozenset[str]:
    folders: set[str] = set()
    for profile in profiles:
        if profile.workflow_template is not None:
            folders.update(
                asset.folder
                for _, asset in user_workflow_assets(profile)
                if asset.folder in _INVENTORY_MODEL_FOLDERS
            )
        else:
            folders.update(asset.folder for asset in required_assets(profile).values())
    return frozenset(folders)


def _inventory_key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def profile_is_installed(profile: ModelProfile, inventory: Mapping[str, Sequence[str]]) -> bool:
    if profile.workflow_template is not None:
        for binding, asset in user_workflow_assets(profile):
            if asset.folder not in _INVENTORY_MODEL_FOLDERS:
                # A direct model can legitimately live in a user-created
                # ComfyUI/models subtree that the public inventory endpoint
                # does not expose.  Runtime object_info still validates its
                # exact loader choice before submission.
                continue
            names = inventory.get(asset.folder)
            if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
                return False
            known = {_inventory_key(name) for name in names if isinstance(name, str)}
            if _inventory_key(binding.comfy_name) not in known:
                return False
        return True
    for asset in required_assets(profile).values():
        names = inventory.get(asset.folder)
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            return False
        known = {_inventory_key(name) for name in names if isinstance(name, str)}
        if _inventory_key(asset.comfy_name) not in known:
            return False
    return True


def select_profile(
    profiles: Iterable[ModelProfile],
    inventory: Mapping[str, Sequence[str]],
    *,
    prompt: str,
    model_hint: str = "",
    selected_profile_id: str | None = None,
) -> tuple[ModelProfile, str]:
    candidates = [profile for profile in profiles if profile_is_installed(profile, inventory)]
    if not candidates:
        raise WorkflowValidationError("등록된 모델 구성요소를 현재 ComfyUI에서 찾을 수 없습니다.")

    # Manual selection is an exact registered profile ID, never an LLM-written
    # name/model hint.  Do not fall back to another installed candidate: that
    # would silently violate the user's explicit model choice.
    if selected_profile_id is not None:
        requested_id = _safe_id(selected_profile_id, "수동 선택 모델 ID")
        selected = next((profile for profile in candidates if profile.id == requested_id), None)
        if selected is None:
            raise WorkflowValidationError(
                "수동으로 선택한 모델은 현재 ComfyUI에서 사용할 수 없습니다. "
                "등록 상태와 필수 구성 파일을 확인해 주세요."
            )
        ignored_count = (
            0
            if selected.workflow_template is not None
            else len(selected.assets) - len(required_assets(selected))
        )
        reason = f"사용자가 수동으로 '{selected.name}' 모델을 선택했습니다."
        if ignored_count:
            reason += f" 등록된 추가 자산 {ignored_count}개는 현재 기본 생성에 적용하지 않았습니다."
        return selected, reason

    hint = model_hint.strip().casefold()
    haystack = re.sub(r"[_-]+", " ", f"{prompt} {model_hint}".casefold())

    def tag_matches_request(tag: str) -> bool:
        normalized = " ".join(re.sub(r"[_-]+", " ", tag.casefold()).split())
        if not normalized:
            return False
        phrase = r"\s+".join(re.escape(part) for part in normalized.split())
        return re.search(rf"(?<!\w){phrase}(?!\w)", haystack) is not None

    def rank(profile: ModelProfile) -> tuple[int, int, int, str, str]:
        exact_hint = int(bool(hint) and hint in {profile.id.casefold(), profile.name.casefold()})
        tag_matches = sum(1 for tag in profile.tags if tag_matches_request(tag))
        return (-exact_hint, -tag_matches, -profile.priority, profile.name.casefold(), profile.id)

    selected = sorted(candidates, key=rank)[0]
    exact_hint = bool(hint) and hint in {selected.id.casefold(), selected.name.casefold()}
    matched_tags = [tag for tag in selected.tags if tag_matches_request(tag)]
    if exact_hint:
        reason = f"모델 힌트가 '{selected.name}'과 정확히 일치했습니다."
    elif matched_tags:
        reason = f"요청과 태그 {', '.join(matched_tags)}가 일치하고 우선순위가 가장 높았습니다."
    else:
        reason = "사용 가능한 모델 중 사용자 지정 우선순위와 안정된 이름 순서로 선택했습니다."
    ignored_count = 0 if selected.workflow_template is not None else len(selected.assets) - len(required_assets(selected))
    if ignored_count:
        reason += f" 등록된 추가 자산 {ignored_count}개는 현재 기본 생성에 적용하지 않았습니다."
    return selected, reason


def apply_prompt_policy(
    profile: ModelProfile,
    *,
    prompt: Any,
    negative_prompt: Any = "",
) -> dict[str, Any]:
    """모델 실행 계약에 맞는 결정론적 품질 제약을 적용한다.

    SD 계열은 실제 negative conditioning에 보수적인 품질·인체 항목을 합치고,
    FLUX 계열은 negative 채널을 쓰지 않으므로 알려진 제외 의도를 긍정적인 시각
    조건으로 바꾼다. 사용자 API 워크플로는 구조를 추측하지 않고 등록된 바인딩만 따른다.
    """
    original = _required_text(prompt, "프롬프트", max_length=MAX_PROMPT_LENGTH)
    if (
        not isinstance(negative_prompt, str)
        or len(negative_prompt) > MAX_PROMPT_LENGTH
        or "\x00" in negative_prompt
    ):
        raise WorkflowValidationError("부정 프롬프트 형식이 올바르지 않습니다.")
    original_negative = negative_prompt.strip()
    if profile.workflow_template is not None:
        negative_connected = bool(profile.workflow_template.bindings["negativePrompt"])
        return {
            "originalPrompt": original,
            "originalNegativePrompt": original_negative,
            "effectivePrompt": original,
            "effectiveNegativePrompt": original_negative if negative_connected else "",
            "promptPolicy": {
                "id": (
                    "user-workflow-pass-through-v1"
                    if negative_connected
                    else "user-workflow-negative-unbound-v1"
                ),
                "label": (
                    "사용자 워크플로 원문 바인딩"
                    if negative_connected
                    else "사용자 워크플로 네거티브 미연결"
                ),
                "description": (
                    "사용자가 등록한 API 워크플로의 긍정·네거티브 입력 바인딩에 원문을 그대로 넣습니다. 선택된 결과 경로 포함 여부는 생성 결과에 별도로 표시합니다."
                    if negative_connected
                    else "사용자 API 워크플로에 네거티브 입력 바인딩이 없어 긍정 프롬프트만 연결하고, 요청한 제외 요소는 기록으로 남깁니다."
                ),
                "addedPositive": [],
                "addedNegative": [],
            },
        }
    positive_text = original.casefold()

    def contains_any(values: Iterable[str], text: str = positive_text) -> bool:
        normalized_text = re.sub(r"[_-]+", " ", text.casefold())
        for value in values:
            normalized = " ".join(re.sub(r"[_-]+", " ", value.casefold()).split())
            if not normalized:
                continue
            phrase = r"\s+".join(re.escape(part) for part in normalized.split())
            if re.search(rf"(?<!\w){phrase}(?!\w)", normalized_text):
                return True
        return False

    def append_terms(base: str, values: Iterable[str]) -> tuple[str, list[str]]:
        result = base
        seen = {" ".join(part.casefold().split()) for part in base.split(",") if part.strip()}
        added: list[str] = []
        for value in values:
            key = " ".join(value.casefold().split())
            if not key or key in seen:
                continue
            candidate = f"{result}, {value}" if result else value
            if len(candidate) > MAX_PROMPT_LENGTH:
                continue
            result = candidate
            seen.add(key)
            added.append(value)
        return result, added

    def append_sentences(base: str, values: Iterable[str]) -> tuple[str, list[str]]:
        result = base
        folded = " ".join(base.casefold().split())
        added: list[str] = []
        for value in values:
            key = " ".join(value.casefold().split())
            if not key or key in folded:
                continue
            candidate = f"{result.rstrip()} {value}" if result else value
            if len(candidate) > MAX_PROMPT_LENGTH:
                continue
            result = candidate
            folded = f"{folded} {key}".strip()
            added.append(value)
        return result, added

    character_markers = (
        "1girl", "1boy", "woman", "man", "girl", "boy", "person", "people",
        "character", "portrait", "human", "hand", "캐릭터", "인물", "사람", "애니메이션",
    )
    character_requested = contains_any(character_markers)

    deliberate_blur = contains_any((
        "blur", "blurry", "blurred", "out of focus", "motion blur", "soft focus",
        "dreamy blur", "blurry background", "bokeh", "depth of field", "intentional blur",
        "의도적인 블러", "흐릿한", "아웃포커싱",
    ))
    deliberate_low_resolution = contains_any((
        "low resolution", "pixel art", "pixelated", "8 bit", "16 bit", "retro sprite",
        "game sprite", "저해상도", "픽셀 아트", "도트 그래픽",
    ))
    deliberate_compression = contains_any((
        "jpeg aesthetic", "compression artifacts", "vhs", "analog noise", "glitch art",
        "datamosh", "found footage", "cctv", "surveillance footage", "압축 노이즈", "글리치",
    ))
    deliberate_low_quality = contains_any((
        "low quality", "lo fi", "amateur photo", "disposable camera", "photocopy",
        "found footage", "cctv", "surveillance footage", "낮은 화질", "로파이",
    )) or deliberate_low_resolution or deliberate_compression
    deliberate_anatomy_distortion = contains_any((
        "body horror", "eldritch", "surreal anatomy", "extra arms", "extra limbs",
        "extra fingers", "mutated body", "deformed body", "anatomical distortion",
        "바디 호러", "추가 팔", "기형적인 신체",
    ))

    if profile.architecture in (ARCH_SD15, ARCH_SDXL):
        negative_terms: list[str] = []
        if not deliberate_low_quality:
            negative_terms.append("low quality")
        if not deliberate_low_resolution:
            negative_terms.append("low resolution")
        if not deliberate_blur:
            negative_terms.append("blurry")
        if not deliberate_compression:
            negative_terms.append("jpeg artifacts")
        if character_requested and not deliberate_anatomy_distortion:
            negative_terms.extend((
                "bad anatomy", "malformed hands", "extra fingers", "missing fingers", "fused fingers",
            ))
        effective_negative, added_negative = append_terms(original_negative, negative_terms)
        return {
            "originalPrompt": original,
            "originalNegativePrompt": original_negative,
            "effectivePrompt": original,
            "effectiveNegativePrompt": effective_negative,
            "promptPolicy": {
                "id": "sd-negative-quality-v1",
                "label": "SD 품질 네거티브",
                "description": "SD 계열의 실제 네거티브 조건에 기본 품질 항목을 합치며, 인물 요청일 때만 손·해부학 항목을 추가합니다.",
                "addedPositive": [],
                "addedNegative": added_negative,
            },
        }

    if profile.architecture in (ARCH_FLUX1_SPLIT, ARCH_FLUX2_KLEIN_4B):
        requested_negative = original_negative.casefold()
        positive_constraints: list[str] = []
        if not deliberate_low_quality and contains_any(
            ("low quality", "low resolution", "pixelated", "noisy", "compression artifacts", "jpeg artifacts"),
            requested_negative,
        ):
            positive_constraints.append("The image has crisp, high-resolution visual detail and clean tonal transitions.")
        if not deliberate_blur and contains_any(("blurry", "blur", "out of focus"), requested_negative):
            positive_constraints.append("The main subject is clearly focused with well-defined details.")
        if character_requested and not deliberate_anatomy_distortion and contains_any(
            ("anatomy", "hand", "hands", "finger", "fingers", "deformed", "malformed"),
            requested_negative,
        ):
            positive_constraints.append(
                "Human anatomy is coherent, with natural hands and clearly separated fingers."
            )
        text_requested = contains_any(
            ("text", "typography", "lettering", "logo", "caption", "title", "sign"),
            positive_text,
        )
        if not text_requested and contains_any(("watermark", "signature", "logo", "text"), requested_negative):
            positive_constraints.append("Surfaces are clean and unmarked.")
        effective_prompt, added_positive = append_sentences(original, positive_constraints)
        if added_positive:
            policy_id = "flux-positive-constraints-v1"
            policy_label = "FLUX 제외 요소 긍정 변환"
            policy_description = "FLUX 내장 워크플로에 네거티브 입력이 없어, 인식 가능한 제외 의도만 사용자 스타일과 충돌하지 않는 긍정 조건으로 바꿉니다."
        elif original_negative:
            policy_id = "flux-negative-unconnected-v1"
            policy_label = "FLUX 네거티브 미연결"
            policy_description = "FLUX 내장 워크플로에 네거티브 입력이 없습니다. 자동으로 안전하게 바꿀 수 없는 제외 요소는 원문 기록만 남기고 프롬프트에 임의로 추가하지 않습니다."
        else:
            policy_id = "flux-positive-only-v1"
            policy_label = "FLUX 긍정 프롬프트 유지"
            policy_description = "FLUX 내장 워크플로의 긍정 프롬프트 입력에 사용자 원문을 변경 없이 연결합니다."
        return {
            "originalPrompt": original,
            "originalNegativePrompt": original_negative,
            "effectivePrompt": effective_prompt,
            "effectiveNegativePrompt": "",
            "promptPolicy": {
                "id": policy_id,
                "label": policy_label,
                "description": policy_description,
                "addedPositive": added_positive,
                "addedNegative": [],
            },
        }

    return {
        "originalPrompt": original,
        "originalNegativePrompt": original_negative,
        "effectivePrompt": original,
        "effectiveNegativePrompt": original_negative,
        "promptPolicy": {
            "id": "model-prompt-pass-through-v1",
            "label": "모델 프롬프트 원문 유지",
            "description": "확인된 모델 입력 계약에 긍정·네거티브 프롬프트 원문을 그대로 연결합니다.",
            "addedPositive": [],
            "addedNegative": [],
        },
    }


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise WorkflowValidationError(f"{field} 값이 허용 범위를 벗어났습니다.")
    return value


def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise WorkflowValidationError(f"{field} 값이 허용 범위를 벗어났습니다.")
    return float(value)


def resolve_generation_options(
    profile: ModelProfile,
    *,
    prompt: Any,
    negative_prompt: Any = "",
    width: Any = None,
    height: Any = None,
    steps: Any = None,
    cfg: Any = None,
    seed: Any,
    sampler: Any = None,
    scheduler: Any = None,
) -> GenerationOptions:
    positive = _required_text(prompt, "프롬프트", max_length=MAX_PROMPT_LENGTH)
    if not isinstance(negative_prompt, str) or len(negative_prompt) > MAX_PROMPT_LENGTH or "\x00" in negative_prompt:
        raise WorkflowValidationError("부정 프롬프트 형식이 올바르지 않습니다.")
    negative = negative_prompt.strip()
    resolved_width = _bounded_int(
        width if width is not None else profile.defaults.width, "너비", MIN_DIMENSION, MAX_DIMENSION
    )
    resolved_height = _bounded_int(
        height if height is not None else profile.defaults.height, "높이", MIN_DIMENSION, MAX_DIMENSION
    )
    if resolved_width % 64 or resolved_height % 64:
        raise WorkflowValidationError("이미지 너비와 높이는 64의 배수여야 합니다.")
    resolved_steps = _bounded_int(
        steps if steps is not None else profile.defaults.steps, "steps", 1, MAX_STEPS
    )
    resolved_cfg = _bounded_float(
        cfg if cfg is not None else profile.defaults.cfg, "CFG", 0.0, MAX_CFG
    )
    if isinstance(seed, str):
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", seed):
            raise WorkflowValidationError("seed 값이 허용 범위를 벗어났습니다.")
        resolved_seed = _bounded_int(int(seed), "seed", 0, MAX_SEED)
    else:
        resolved_seed = _bounded_int(seed, "seed", 0, MAX_SEED)
    default_scheduler = "simple" if profile.architecture == ARCH_FLUX1_SPLIT else "normal"
    resolved_sampler = sampler if sampler is not None else (profile.defaults.sampler or "euler")
    resolved_scheduler = scheduler if scheduler is not None else (profile.defaults.scheduler or default_scheduler)
    if (
        not isinstance(resolved_sampler, str)
        or not 1 <= len(resolved_sampler) <= 80
        or "\x00" in resolved_sampler
        or (profile.workflow_template is None and resolved_sampler not in TRUSTED_SAMPLERS)
    ):
        raise WorkflowValidationError("지원하지 않는 sampler입니다.")
    if (
        not isinstance(resolved_scheduler, str)
        or not 1 <= len(resolved_scheduler) <= 80
        or "\x00" in resolved_scheduler
        or (profile.workflow_template is None and resolved_scheduler not in TRUSTED_SCHEDULERS)
    ):
        raise WorkflowValidationError("지원하지 않는 scheduler입니다.")
    return GenerationOptions(
        prompt=positive,
        negative_prompt=negative,
        width=resolved_width,
        height=resolved_height,
        steps=resolved_steps,
        cfg=resolved_cfg,
        seed=resolved_seed,
        sampler=resolved_sampler,
        scheduler=resolved_scheduler,
    )


def _required_input_map(node_class: str, info: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = info.get("input")
    required = inputs.get("required") if isinstance(inputs, Mapping) else None
    if not isinstance(required, Mapping):
        raise WorkflowValidationError(f"ComfyUI 노드 입력 계약이 올바르지 않습니다: {node_class}")
    return required


def validate_node_contracts(architecture: str, node_infos: Mapping[str, Mapping[str, Any]]) -> None:
    contracts = node_contracts_for_architecture(architecture)
    if set(node_infos) != set(contracts):
        missing = sorted(set(contracts) - set(node_infos))
        raise WorkflowValidationError(f"필수 ComfyUI 노드가 없습니다: {', '.join(missing)}")
    for node_class, contract in contracts.items():
        info = node_infos[node_class]
        if info.get("name") not in (None, node_class):
            raise WorkflowValidationError(f"ComfyUI 노드 이름 계약이 다릅니다: {node_class}")
        if info.get("python_module") != contract.module or info.get("api_node") is True:
            raise WorkflowValidationError(f"신뢰할 수 없는 ComfyUI 노드 구현입니다: {node_class}")
        required = _required_input_map(node_class, info)
        if not contract.required_inputs.issubset(required.keys()):
            raise WorkflowValidationError(f"ComfyUI 노드 입력 계약이 다릅니다: {node_class}")


def _enum_values(node_class: str, info: Mapping[str, Any], input_name: str) -> frozenset[str]:
    definition = _required_input_map(node_class, info).get(input_name)
    if not isinstance(definition, Sequence) or isinstance(definition, (str, bytes)) or not definition:
        raise WorkflowValidationError(f"ComfyUI 노드 선택값 계약이 다릅니다: {node_class}.{input_name}")

    # ComfyUI object_info에는 두 형태가 공존한다.
    # - loader: [["model-a", "model-b"], {...}]
    # - KSamplerSelect 등 COMBO: ["COMBO", {"options": ["euler", ...]}]
    # 어느 쪽이든 서버가 실제로 제공한 문자열 목록만 신뢰한다.
    if definition[0] == "COMBO":
        if len(definition) < 2 or not isinstance(definition[1], Mapping):
            raise WorkflowValidationError(f"ComfyUI 노드 선택값 계약이 다릅니다: {node_class}.{input_name}")
        values = definition[1].get("options")
    else:
        values = definition[0]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise WorkflowValidationError(f"ComfyUI 노드 선택값 계약이 다릅니다: {node_class}.{input_name}")
    if any(not isinstance(value, str) for value in values):
        raise WorkflowValidationError(f"ComfyUI 노드 선택값 계약이 다릅니다: {node_class}.{input_name}")
    return frozenset(values)


def _ensure_numeric_supported(
    node_class: str,
    info: Mapping[str, Any],
    input_name: str,
    value: int | float,
) -> None:
    definition = _required_input_map(node_class, info).get(input_name)
    if (
        not isinstance(definition, Sequence)
        or isinstance(definition, (str, bytes))
        or len(definition) < 2
        or definition[0] not in {"INT", "FLOAT"}
        or not isinstance(definition[1], Mapping)
    ):
        raise WorkflowValidationError(f"ComfyUI 숫자 입력 계약이 다릅니다: {node_class}.{input_name}")
    metadata = definition[1]
    minimum = metadata.get("min")
    maximum = metadata.get("max")
    if (
        not isinstance(minimum, (int, float))
        or isinstance(minimum, bool)
        or not isinstance(maximum, (int, float))
        or isinstance(maximum, bool)
        or not float(minimum) <= float(value) <= float(maximum)
    ):
        raise WorkflowValidationError(
            f"현재 ComfyUI가 {node_class}.{input_name}={value} 값을 지원하지 않습니다."
        )


def _definition_choices(definition: Any) -> frozenset[str] | None:
    if not isinstance(definition, Sequence) or isinstance(definition, (str, bytes)) or not definition:
        return None
    values: Any
    if definition[0] == "COMBO":
        if len(definition) < 2 or not isinstance(definition[1], Mapping):
            return None
        values = definition[1].get("options")
    elif isinstance(definition[0], Sequence) and not isinstance(definition[0], (str, bytes)):
        values = definition[0]
    else:
        return None
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    if any(not isinstance(value, str) for value in values):
        return None
    return frozenset(values)


def _build_user_workflow(
    profile: ModelProfile, options: GenerationOptions, filename_prefix: str
) -> dict[str, dict[str, Any]]:
    template = profile.workflow_template
    if template is None:
        raise WorkflowValidationError("사용자 워크플로가 연결되지 않았습니다.")
    workflow = copy.deepcopy(template.graph)
    values: dict[str, Any] = {
        "positivePrompt": options.prompt,
        "negativePrompt": options.negative_prompt,
        "seed": options.seed,
        "width": options.width,
        "height": options.height,
        "steps": options.steps,
        "cfg": options.cfg,
        "sampler": options.sampler,
        "scheduler": options.scheduler,
        "filenamePrefix": filename_prefix,
    }
    for target, refs in template.bindings.items():
        for node_id, input_name in refs:
            workflow[node_id]["inputs"][input_name] = values[target]
    # Re-assert the registered model contract on the copied graph.  A literal
    # loader name is never trusted merely because an identically named model is
    # installed elsewhere in ComfyUI.
    for binding, _asset in user_workflow_assets(profile):
        workflow[binding.node_id]["inputs"][binding.input_name] = binding.comfy_name
    return workflow


def _validate_user_workflow_runtime(
    profile: ModelProfile,
    options: GenerationOptions,
    node_infos: Mapping[str, Mapping[str, Any]],
) -> None:
    expected_classes = node_classes_for_profile(profile)
    if set(node_infos) != set(expected_classes):
        missing = sorted(set(expected_classes) - set(node_infos))
        raise WorkflowValidationError(f"사용자 워크플로에 필요한 ComfyUI 노드가 없습니다: {', '.join(missing)}")
    workflow = _build_user_workflow(
        profile, options, "Aiso/00000000-0000-4000-8000-000000000000"
    )
    for node_id, node in workflow.items():
        class_type = node["class_type"]
        info = node_infos[class_type]
        module = info.get("python_module")
        if (
            info.get("name") not in (None, class_type)
            or info.get("api_node") is True
            or not isinstance(module, str)
            or (module != "nodes" and not module.startswith("comfy_extras."))
        ):
            raise WorkflowValidationError(
                f"사용자 워크플로는 ComfyUI 기본 노드만 Agent에서 실행할 수 있습니다: {class_type}"
            )
        input_info = info.get("input")
        required = input_info.get("required") if isinstance(input_info, Mapping) else None
        optional = input_info.get("optional", {}) if isinstance(input_info, Mapping) else {}
        if not isinstance(required, Mapping) or not isinstance(optional, Mapping):
            raise WorkflowValidationError(f"ComfyUI 노드 입력 계약이 올바르지 않습니다: {class_type}")
        inputs = node["inputs"]
        if not set(required).issubset(inputs) or not set(inputs).issubset({*required, *optional}):
            raise WorkflowValidationError(
                f"사용자 워크플로 노드 입력 계약이 다릅니다: {class_type} ({node_id})"
            )
        definitions = {**optional, **required}
        for input_name, value in inputs.items():
            if isinstance(value, list):
                if (
                    len(value) != 2
                    or not isinstance(value[0], str)
                    or value[0] not in workflow
                    or not isinstance(value[1], int)
                    or isinstance(value[1], bool)
                    or not 0 <= value[1] <= 32
                ):
                    raise WorkflowValidationError(
                        f"사용자 워크플로 노드 연결이 올바르지 않습니다: {class_type}.{input_name}"
                    )
                continue
            definition = definitions[input_name]
            choices = _definition_choices(definition)
            if choices is not None and (not isinstance(value, str) or value not in choices):
                raise WorkflowValidationError(
                    f"현재 ComfyUI가 사용자 워크플로 선택값을 지원하지 않습니다: {class_type}.{input_name}"
                )
            if (
                isinstance(definition, Sequence)
                and not isinstance(definition, (str, bytes))
                and definition
                and isinstance(definition[0], str)
                and definition[0] in {"INT", "FLOAT"}
            ):
                expected_type = definition[0]
                valid_type = (
                    isinstance(value, int) and not isinstance(value, bool)
                    if expected_type == "INT"
                    else isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
                )
                if not valid_type:
                    raise WorkflowValidationError(
                        f"사용자 워크플로 숫자 입력 형식이 올바르지 않습니다: {class_type}.{input_name}"
                    )
                metadata = definition[1] if len(definition) > 1 and isinstance(definition[1], Mapping) else {}
                minimum = metadata.get("min")
                maximum = metadata.get("max")
                if (
                    isinstance(minimum, (int, float))
                    and isinstance(maximum, (int, float))
                    and not float(minimum) <= float(value) <= float(maximum)
                ):
                    raise WorkflowValidationError(
                        f"현재 ComfyUI가 {class_type}.{input_name}={value} 값을 지원하지 않습니다."
                    )


def validate_runtime_options(
    profile: ModelProfile,
    options: GenerationOptions,
    node_infos: Mapping[str, Mapping[str, Any]],
) -> None:
    """서버가 실제로 노출하는 모델·sampler·scheduler 선택값과 대조한다."""
    if profile.workflow_template is not None:
        _validate_user_workflow_runtime(profile, options, node_infos)
        return
    validate_node_contracts(profile.architecture, node_infos)
    assets = required_assets(profile)
    if profile.architecture in (ARCH_SD15, ARCH_SDXL):
        checks = [
            ("CheckpointLoaderSimple", "ckpt_name", assets["checkpoint"].comfy_name),
            ("KSampler", "sampler_name", options.sampler),
            ("KSampler", "scheduler", options.scheduler),
        ]
        numeric_checks = [
            ("EmptyLatentImage", "width", options.width),
            ("EmptyLatentImage", "height", options.height),
            ("KSampler", "seed", options.seed),
            ("KSampler", "steps", options.steps),
            ("KSampler", "cfg", options.cfg),
        ]
    elif profile.architecture == ARCH_FLUX1_SPLIT:
        checks = [
            ("UNETLoader", "unet_name", assets["diffusion_model"].comfy_name),
            ("DualCLIPLoader", "clip_name1", assets["clip_l"].comfy_name),
            ("DualCLIPLoader", "clip_name2", assets["t5xxl"].comfy_name),
            ("DualCLIPLoader", "type", "flux"),
            ("VAELoader", "vae_name", assets["vae"].comfy_name),
            ("KSamplerSelect", "sampler_name", options.sampler),
            ("BasicScheduler", "scheduler", options.scheduler),
            ("UNETLoader", "weight_dtype", "default"),
        ]
        numeric_checks = [
            ("EmptySD3LatentImage", "width", options.width),
            ("EmptySD3LatentImage", "height", options.height),
            ("RandomNoise", "noise_seed", options.seed),
            ("BasicScheduler", "steps", options.steps),
            ("FluxGuidance", "guidance", options.cfg),
        ]
    else:
        checks = [
            ("UNETLoader", "unet_name", assets["diffusion_model"].comfy_name),
            ("UNETLoader", "weight_dtype", "default"),
            ("CLIPLoader", "clip_name", assets["qwen3"].comfy_name),
            ("CLIPLoader", "type", "flux2"),
            ("VAELoader", "vae_name", assets["vae"].comfy_name),
            ("KSamplerSelect", "sampler_name", options.sampler),
        ]
        numeric_checks = [
            ("Flux2Scheduler", "steps", options.steps),
            ("Flux2Scheduler", "width", options.width),
            ("Flux2Scheduler", "height", options.height),
            ("EmptyFlux2LatentImage", "width", options.width),
            ("EmptyFlux2LatentImage", "height", options.height),
            ("CFGGuider", "cfg", options.cfg),
            ("RandomNoise", "noise_seed", options.seed),
        ]
    for node_class, input_name, value in checks:
        if value not in _enum_values(node_class, node_infos[node_class], input_name):
            raise WorkflowValidationError(
                f"현재 ComfyUI가 '{value}' 선택값을 지원하지 않습니다: {node_class}.{input_name}"
            )
    for node_class, input_name, value in numeric_checks:
        _ensure_numeric_supported(node_class, node_infos[node_class], input_name, value)


def _build_sd_workflow(
    profile: ModelProfile, options: GenerationOptions, filename_prefix: str
) -> dict[str, dict[str, Any]]:
    checkpoint = required_assets(profile)["checkpoint"].comfy_name
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": options.prompt, "clip": ["1", 1]}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": options.negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": options.width, "height": options.height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "seed": options.seed,
                "steps": options.steps,
                "cfg": options.cfg,
                "sampler_name": options.sampler,
                "scheduler": options.scheduler,
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": filename_prefix},
        },
    }


def _build_flux_workflow(
    profile: ModelProfile, options: GenerationOptions, filename_prefix: str
) -> dict[str, dict[str, Any]]:
    assets = required_assets(profile)
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": assets["diffusion_model"].comfy_name,
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": assets["clip_l"].comfy_name,
                "clip_name2": assets["t5xxl"].comfy_name,
                "type": "flux",
            },
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": assets["vae"].comfy_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": options.prompt, "clip": ["2", 0]}},
        "5": {
            "class_type": "FluxGuidance",
            "inputs": {"conditioning": ["4", 0], "guidance": options.cfg},
        },
        "6": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["5", 0]}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": options.seed}},
        "8": {
            "class_type": "BasicScheduler",
            "inputs": {
                "model": ["1", 0],
                "scheduler": options.scheduler,
                "steps": options.steps,
                "denoise": 1.0,
            },
        },
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": options.sampler}},
        "10": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": options.width, "height": options.height, "batch_size": 1},
        },
        "11": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["7", 0],
                "guider": ["6", 0],
                "sampler": ["9", 0],
                "sigmas": ["8", 0],
                "latent_image": ["10", 0],
            },
        },
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {
            "class_type": "SaveImage",
            "inputs": {"images": ["12", 0], "filename_prefix": filename_prefix},
        },
    }


def _build_flux2_klein_workflow(
    profile: ModelProfile, options: GenerationOptions, filename_prefix: str
) -> dict[str, dict[str, Any]]:
    """FLUX.2 [klein] 4B distilled text-to-image 공식 노드 구성을 만든다.

    FLUX.2는 일반 scheduler 문자열이나 FLUX.1의 DualCLIPLoader/FluxGuidance를
    사용하지 않는다. Qwen 3을 ``flux2`` 타입으로 로드하고 Flux2Scheduler가
    해상도와 4-step 설정으로 sigma를 계산한다.
    """
    assets = required_assets(profile)
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": assets["diffusion_model"].comfy_name, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": assets["qwen3"].comfy_name, "type": "flux2"},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": assets["vae"].comfy_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": options.prompt, "clip": ["2", 0]}},
        # 공식 Klein 4B 템플릿은 positive conditioning을 zero-out해 negative에 연결한다.
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {
            "class_type": "CFGGuider",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "cfg": options.cfg,
            },
        },
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": options.seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": options.sampler}},
        "9": {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": options.steps, "width": options.width, "height": options.height},
        },
        "10": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": options.width, "height": options.height, "batch_size": 1},
        },
        "11": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["7", 0],
                "guider": ["6", 0],
                "sampler": ["8", 0],
                "sigmas": ["9", 0],
                "latent_image": ["10", 0],
            },
        },
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": filename_prefix}},
    }


def build_workflow(
    profile: ModelProfile,
    options: GenerationOptions,
    *,
    prompt_id: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(prompt_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prompt_id
    ):
        raise WorkflowValidationError("ComfyUI prompt ID 형식이 올바르지 않습니다.")
    filename_prefix = f"Aiso/{prompt_id}"
    if profile.workflow_template is not None:
        workflow = _build_user_workflow(profile, options, filename_prefix)
    elif profile.architecture in (ARCH_SD15, ARCH_SDXL):
        workflow = _build_sd_workflow(profile, options, filename_prefix)
    elif profile.architecture == ARCH_FLUX1_SPLIT:
        workflow = _build_flux_workflow(profile, options, filename_prefix)
    elif profile.architecture == ARCH_FLUX2_KLEIN_4B:
        workflow = _build_flux2_klein_workflow(profile, options, filename_prefix)
    else:
        raise WorkflowValidationError("지원하지 않는 모델 아키텍처입니다.")
    allowed = set(node_classes_for_profile(profile))
    for node_id, node in workflow.items():
        if not _SAFE_NODE_ID_RE.fullmatch(node_id) or node.get("class_type") not in allowed:
            raise WorkflowValidationError("신뢰되지 않은 ComfyUI 워크플로 노드입니다.")
    return workflow


def _snapshot_input_value(
    value: Any, node_ids: frozenset[str], *, allow_literal_arrays: bool = False
) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_PROMPT_LENGTH or "\x00" in value:
            raise WorkflowValidationError("워크플로 문자열 입력 형식이 올바르지 않습니다.")
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowValidationError("워크플로 숫자 입력 형식이 올바르지 않습니다.")
        return value
    if isinstance(value, list):
        is_connection = (
            len(value) == 2
            and isinstance(value[0], str)
            and value[0] in node_ids
            and isinstance(value[1], int)
            and not isinstance(value[1], bool)
            and 0 <= value[1] <= 32
        )
        if is_connection:
            return [value[0], value[1]]
        if allow_literal_arrays and len(value) <= 256:
            return [
                _snapshot_input_value(item, node_ids, allow_literal_arrays=True)
                for item in value
            ]
        raise WorkflowValidationError("워크플로 노드 연결 형식이 올바르지 않습니다.")
    raise WorkflowValidationError("워크플로 입력 형식이 올바르지 않습니다.")


def snapshot_workflow(
    workflow: Mapping[str, Mapping[str, Any]],
    *,
    allow_user_template: bool = False,
) -> dict[str, dict[str, Any]]:
    """실제 제출 API 그래프에서 안전한 node/class/inputs snapshot만 복제한다.

    신뢰 템플릿의 정확한 input 집합만 허용하므로 client ID, token, 임의 경로 같은
    별도 필드는 결과 계약으로 유출될 수 없다. 노드 연결 배열도 그대로 보존한다.
    """
    if not isinstance(workflow, Mapping) or not workflow:
        raise WorkflowValidationError("워크플로 snapshot 형식이 올바르지 않습니다.")
    node_ids = frozenset(workflow)
    if any(not isinstance(node_id, str) or not _SAFE_NODE_ID_RE.fullmatch(node_id) for node_id in node_ids):
        raise WorkflowValidationError("워크플로 snapshot 노드 ID가 올바르지 않습니다.")

    contracts = {**SD_NODE_CONTRACTS, **FLUX_NODE_CONTRACTS, **FLUX2_KLEIN_NODE_CONTRACTS}
    asset_inputs = {"ckpt_name", "unet_name", "clip_name1", "clip_name2", "clip_name", "vae_name"}
    snapshot: dict[str, dict[str, Any]] = {}
    for node_id, raw_node in workflow.items():
        if not isinstance(raw_node, Mapping) or set(raw_node) != {"class_type", "inputs"}:
            raise WorkflowValidationError("워크플로 snapshot 노드 형식이 올바르지 않습니다.")
        class_type = raw_node.get("class_type")
        inputs = raw_node.get("inputs")
        contract = contracts.get(class_type) if isinstance(class_type, str) else None
        if (contract is None and not allow_user_template) or not isinstance(inputs, Mapping):
            raise WorkflowValidationError("신뢰되지 않은 워크플로 snapshot 노드입니다.")
        # User templates are already validated against the live ComfyUI node
        # contract. Do not reject a valid optional input merely because the
        # built-in snapshot contract is intentionally narrower.
        if contract is not None and not allow_user_template and set(inputs) != set(contract.required_inputs):
            raise WorkflowValidationError("워크플로 snapshot 입력 계약이 올바르지 않습니다.")
        if contract is None and (
            not isinstance(class_type, str)
            or not _SAFE_NODE_CLASS_RE.fullmatch(class_type)
            or len(inputs) > MAX_USER_WORKFLOW_INPUTS
        ):
            raise WorkflowValidationError("사용자 워크플로 snapshot 노드가 올바르지 않습니다.")
        copied_inputs: dict[str, Any] = {}
        for input_name, value in inputs.items():
            if not isinstance(input_name, str) or not _SAFE_INPUT_NAME_RE.fullmatch(input_name):
                raise WorkflowValidationError("워크플로 snapshot 입력 이름이 올바르지 않습니다.")
            if input_name in asset_inputs or _is_model_loader_input_name(input_name):
                _safe_relative_name(value, f"{class_type}.{input_name}")
            elif input_name == "filename_prefix":
                if not isinstance(value, str) or not re.fullmatch(
                    r"Aiso/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    value,
                ):
                    raise WorkflowValidationError("출력 파일 prefix가 올바르지 않습니다.")
            copied_inputs[input_name] = _snapshot_input_value(
                value, node_ids, allow_literal_arrays=allow_user_template
            )
        snapshot[node_id] = {"class_type": class_type, "inputs": copied_inputs}
    return snapshot


def primary_model_name(profile: ModelProfile) -> str:
    if profile.workflow_template is not None:
        return user_workflow_assets(profile)[0][0].comfy_name
    assets = required_assets(profile)
    key = "checkpoint" if profile.architecture in (ARCH_SD15, ARCH_SDXL) else "diffusion_model"
    return assets[key].comfy_name


__all__ = [
    "ARCH_FLUX1_SPLIT",
    "ARCH_FLUX2_KLEIN_4B",
    "ARCH_USER_API",
    "ARCH_SD15",
    "ARCH_SDXL",
    "CAPABILITY_TEXT_TO_IMAGE",
    "FLUX_NODE_CONTRACTS",
    "FLUX2_KLEIN_NODE_CONTRACTS",
    "GenerationOptions",
    "ModelAsset",
    "ModelProfile",
    "WorkflowAssetBinding",
    "SD_NODE_CONTRACTS",
    "SUPPORTED_ARCHITECTURES",
    "WorkflowValidationError",
    "apply_prompt_policy",
    "build_workflow",
    "inventory_folders",
    "node_contracts_for_architecture",
    "node_classes_for_profile",
    "normalize_asset",
    "normalize_profile",
    "normalize_profiles",
    "primary_model_name",
    "profile_is_installed",
    "required_assets",
    "resolve_generation_options",
    "select_profile",
    "snapshot_workflow",
    "user_workflow_assets",
    "validate_node_contracts",
    "validate_runtime_options",
]
