"""신뢰된 ComfyUI 텍스트→이미지 워크플로와 사용자 모델 프로필 검증.

LLM이나 렌더러가 ``class_type`` 또는 노드 연결을 전달하지 않는다. Aiso가
소유한 두 템플릿(SD 체크포인트, FLUX.1 split)만 API 형식으로 새로 만든다.
모델 파일은 ComfyUI에 있고 이 모듈은 사용자 등록 메타데이터만 다룬다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


CAPABILITY_TEXT_TO_IMAGE = "text-to-image"
ARCH_SD15 = "sd15"
ARCH_SDXL = "sdxl"
ARCH_FLUX1_SPLIT = "flux1-split"
SUPPORTED_ARCHITECTURES = frozenset({ARCH_SD15, ARCH_SDXL, ARCH_FLUX1_SPLIT})

MAX_PROMPT_LENGTH = 4_000
MIN_DIMENSION = 256
MAX_DIMENSION = 2_048
MAX_STEPS = 60
MAX_CFG = 30.0
MAX_SEED = 2**64 - 1

TRUSTED_SAMPLERS = frozenset({"euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde"})
TRUSTED_SCHEDULERS = frozenset({"normal", "karras", "simple", "beta"})

# cagliostrolab/animagine-xl-4.0 공식 모델 카드의 공통 v4.0/Opt 권장값이다.
# 파일명 추측만으로 정책을 켜지 않도록 아래 hash 또는 명시적 프로필 정체성과 함께 사용한다.
ANIMAGINE_XL_4_POLICY_ID = "cagliostrolab.animagine-xl-4.0.tag-style.v1"
ANIMAGINE_XL_4_POLICY_LABEL = "Animagine XL 4.0 / Opt 공식 태그 정책"
ANIMAGINE_XL_4_POSITIVE_TAGS = (
    "masterpiece",
    "high score",
    "great score",
    "absurdres",
)
ANIMAGINE_XL_4_NEGATIVE_TAGS = (
    "lowres",
    "bad anatomy",
    "bad hands",
    "text",
    "error",
    "missing finger",
    "extra digits",
    "fewer digits",
    "cropped",
    "worst quality",
    "low quality",
    "low score",
    "bad score",
    "average score",
    "signature",
    "watermark",
    "username",
    "blurry",
)

_ANIMAGINE_XL_4_OFFICIAL_HASHES = {
    "1d5b43ff75b6ab598502d4c779d2fbfa3dceca51c60c3b609640a60772333916": "4.0",
    "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac": "4.0 Opt",
}
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_NODE_ID_RE = re.compile(r"^[0-9]{1,3}$")

_ARCH_ALIASES = {
    "sd15": ARCH_SD15,
    "sd1.5": ARCH_SD15,
    "sd-1.5": ARCH_SD15,
    "sdxl": ARCH_SDXL,
    "sd-xl": ARCH_SDXL,
    "flux1-split": ARCH_FLUX1_SPLIT,
    "flux1_split": ARCH_FLUX1_SPLIT,
    "flux.1-split": ARCH_FLUX1_SPLIT,
}
_FAMILY_TO_ARCHITECTURE = {
    "sd15": ARCH_SD15,
    "sdxl": ARCH_SDXL,
    "flux1": ARCH_FLUX1_SPLIT,
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


class WorkflowValidationError(ValueError):
    """프로필, 생성 입력 또는 ComfyUI 노드 계약이 안전 기준을 벗어남."""


@dataclass(frozen=True)
class ModelAsset:
    id: str
    kind: str
    slot: str
    file_name: str
    comfy_name: str
    relative_path: str
    size: int
    sha256: str

    @property
    def folder(self) -> str:
        return _ASSET_FOLDERS[self.kind]


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


@dataclass(frozen=True)
class PromptPolicyMatch:
    id: str
    label: str
    description: str


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


def node_contracts_for_architecture(architecture: str) -> dict[str, NodeContract]:
    if architecture in (ARCH_SD15, ARCH_SDXL):
        return SD_NODE_CONTRACTS
    if architecture == ARCH_FLUX1_SPLIT:
        return FLUX_NODE_CONTRACTS
    raise WorkflowValidationError("지원하지 않는 모델 아키텍처입니다.")


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
    }
    normalized = aliases.get(slot, slot)
    if kind == "text_encoder" and normalized not in {"clip_l", "t5xxl"}:
        raise WorkflowValidationError("텍스트 인코더 slot은 clip_l 또는 t5xxl이어야 합니다.")
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


def _normalize_profile_defaults(raw: Any, architecture: str) -> ProfileDefaults:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("모델 생성 기본값 형식이 올바르지 않습니다.")
    fallback_dimension = 512 if architecture == ARCH_SD15 else 1024
    fallback_cfg = 3.5 if architecture == ARCH_FLUX1_SPLIT else 7.0
    width = _bounded_int(raw.get("width", fallback_dimension), "기본 너비", MIN_DIMENSION, MAX_DIMENSION)
    height = _bounded_int(raw.get("height", fallback_dimension), "기본 높이", MIN_DIMENSION, MAX_DIMENSION)
    if width % 64 or height % 64:
        raise WorkflowValidationError("기본 이미지 너비와 높이는 64의 배수여야 합니다.")
    steps = _bounded_int(raw.get("steps", 20), "기본 steps", 1, MAX_STEPS)
    cfg = _bounded_float(raw.get("cfg", fallback_cfg), "기본 CFG", 0.0, MAX_CFG)
    sampler = raw.get("sampler")
    scheduler = raw.get("scheduler")
    if sampler is not None and sampler not in TRUSTED_SAMPLERS:
        raise WorkflowValidationError("모델 기본 sampler가 지원 범위를 벗어났습니다.")
    if scheduler is not None and scheduler not in TRUSTED_SCHEDULERS:
        raise WorkflowValidationError("모델 기본 scheduler가 지원 범위를 벗어났습니다.")
    return ProfileDefaults(width, height, steps, cfg, sampler, scheduler)


def normalize_asset(raw: Mapping[str, Any]) -> ModelAsset:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("모델 구성요소 형식이 올바르지 않습니다.")
    asset_id = _safe_id(raw.get("id"), "모델 구성요소 ID")
    kind = raw.get("kind")
    if kind not in _ASSET_FOLDERS:
        raise WorkflowValidationError("지원하지 않는 모델 구성요소 종류입니다.")
    slot = _normalize_slot(kind, raw.get("slot"))
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


def normalize_profile(raw: Mapping[str, Any]) -> ModelProfile:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("모델 프로필 형식이 올바르지 않습니다.")
    profile_id = _safe_id(raw.get("id"), "모델 프로필 ID")
    name_raw = raw.get("name", raw.get("displayName"))
    name = _required_text(name_raw, "모델 프로필 이름", max_length=120)
    family_raw = raw.get("family")
    architecture_raw = raw.get("architecture")
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
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise WorkflowValidationError("지원하지 않는 모델 아키텍처입니다.")
    if raw.get("agentEnabled") is not True:
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
    }[architecture]
    expected_template = f"{family_for_template}.txt2img.v1"
    workflow_template_id = raw.get("workflowTemplateId", expected_template)
    if workflow_template_id != expected_template:
        raise WorkflowValidationError("Agent가 신뢰하지 않는 워크플로 템플릿 ID입니다.")
    defaults = _normalize_profile_defaults(raw.get("defaults"), architecture)

    profile = ModelProfile(
        id=profile_id,
        name=name,
        architecture=architecture,
        agent_enabled=True,
        capabilities=frozenset(capabilities),
        tags=tuple(tags),
        priority=priority,
        assets=assets,
        workflow_template_id=workflow_template_id,
        defaults=defaults,
    )
    required_assets(profile)  # 빠진 slot과 현재 템플릿이 무시할 구성요소를 함께 거부한다.
    return profile


def required_assets(profile: ModelProfile) -> dict[str, ModelAsset]:
    if profile.architecture in (ARCH_SD15, ARCH_SDXL):
        expected = {"checkpoint"}
    elif profile.architecture == ARCH_FLUX1_SPLIT:
        expected = {
            "diffusion_model",
            "clip_l",
            "t5xxl",
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


def normalize_profiles(profiles: Iterable[Mapping[str, Any]]) -> list[ModelProfile]:
    if isinstance(profiles, (str, bytes, Mapping)):
        raise WorkflowValidationError("모델 프로필 목록 형식이 올바르지 않습니다.")
    normalized: list[ModelProfile] = []
    for raw in profiles:
        try:
            normalized.append(normalize_profile(raw))
        except WorkflowValidationError:
            # 여러 사용자 프로필 중 비활성·미완성 프로필 하나가 전체 자동 선택을 막지 않게 한다.
            continue
    if not normalized:
        raise WorkflowValidationError("Agent가 사용할 수 있는 완전한 모델 프로필이 없습니다.")
    if len({profile.id for profile in normalized}) != len(normalized):
        raise WorkflowValidationError("모델 프로필 ID가 중복되었습니다.")
    return normalized


def inventory_folders(profiles: Iterable[ModelProfile]) -> frozenset[str]:
    return frozenset(asset.folder for profile in profiles for asset in required_assets(profile).values())


def _inventory_key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def profile_is_installed(profile: ModelProfile, inventory: Mapping[str, Sequence[str]]) -> bool:
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
) -> tuple[ModelProfile, str]:
    candidates = [profile for profile in profiles if profile_is_installed(profile, inventory)]
    if not candidates:
        raise WorkflowValidationError("등록된 모델 구성요소를 현재 ComfyUI에서 찾을 수 없습니다.")
    hint = model_hint.strip().casefold()
    haystack = f"{prompt} {model_hint}".casefold()

    def rank(profile: ModelProfile) -> tuple[int, int, int, str, str]:
        exact_hint = int(bool(hint) and hint in {profile.id.casefold(), profile.name.casefold()})
        tag_matches = sum(1 for tag in profile.tags if tag and tag in haystack)
        return (-exact_hint, -tag_matches, -profile.priority, profile.name.casefold(), profile.id)

    selected = sorted(candidates, key=rank)[0]
    exact_hint = bool(hint) and hint in {selected.id.casefold(), selected.name.casefold()}
    matched_tags = [tag for tag in selected.tags if tag and tag in haystack]
    if exact_hint:
        reason = f"모델 힌트가 '{selected.name}'과 정확히 일치했습니다."
    elif matched_tags:
        reason = f"요청과 태그 {', '.join(matched_tags)}가 일치하고 우선순위가 가장 높았습니다."
    else:
        reason = "사용 가능한 모델 중 사용자 지정 우선순위와 안정된 이름 순서로 선택했습니다."
    ignored_count = len(selected.assets) - len(required_assets(selected))
    if ignored_count:
        reason += f" 등록된 추가 자산 {ignored_count}개는 현재 기본 생성에 적용하지 않았습니다."
    return selected, reason


def prompt_policy_match_for_profile(profile: ModelProfile) -> PromptPolicyMatch | None:
    """명시적이고 재현 가능한 근거로 Animagine XL 4.0/Opt만 식별한다.

    SDXL 여부를 먼저 고정하고 공식 체크포인트 SHA-256만 인정한다. 사용자 선택용
    자유 태그, 프로필 표시 이름, 체크포인트 파일명은 모델 정체성 증거로 사용하지 않는다.
    """
    if profile.architecture != ARCH_SDXL:
        return None

    checkpoint = required_assets(profile)["checkpoint"]
    official_variant = _ANIMAGINE_XL_4_OFFICIAL_HASHES.get(checkpoint.sha256)
    if official_variant is None:
        return None
    return PromptPolicyMatch(
        id=ANIMAGINE_XL_4_POLICY_ID,
        label=ANIMAGINE_XL_4_POLICY_LABEL,
        description=(
            f"Animagine XL {official_variant} 식별 근거: 공식 체크포인트 SHA-256 일치. "
            "공식 tag-based prompt 구조를 따르고 quality 태그는 positive 끝에, "
            "공식 권장 negative 태그는 사용자 지정 항목을 보존한 채 적용했습니다."
        ),
    )


def prompt_policy_hint_for_profile(profile: ModelProfile) -> dict[str, str] | None:
    """Agent 사전 안내용 Animagine 프롬프트 작성 힌트."""
    match = prompt_policy_match_for_profile(profile)
    if match is None:
        return None
    return {
        "id": match.id,
        "label": match.label,
        "instructions": (
            "positive prompt를 영어 쉼표 구분 태그로 작성하고 산문 문장을 쓰지 마세요. "
            "해당하면 1girl, 1boy 또는 1other를 맨 앞에 두고, 이어서 캐릭터명, "
            "작품명, rating, 나머지 묘사 태그를 배치하세요. 공식 quality 및 negative "
            "태그는 실행 시 자동으로 중복 없이 추가됩니다."
        ),
    }


def prompt_policy_hint_for_raw_profile(raw: Mapping[str, Any]) -> dict[str, str] | None:
    """검증 전 TS profile에도 안전하게 사용할 수 있는 Agent 정책 힌트."""
    try:
        profile = normalize_profile(raw)
    except (WorkflowValidationError, TypeError):
        return None
    return prompt_policy_hint_for_profile(profile)


def _prompt_tag_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().replace("_", " ")).casefold()


def _split_unique_prompt_tags(value: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for fragment in re.split(r"[,;\n]+", value):
        tag = fragment.strip()
        if not tag:
            continue
        key = _prompt_tag_key(tag)
        if key not in seen:
            tags.append(tag)
            seen.add(key)
    return tags


def _canonical_suffix(
    source: list[str],
    canonical: Sequence[str],
) -> tuple[list[str], list[str]]:
    canonical_keys = {_prompt_tag_key(tag) for tag in canonical}
    source_keys = {_prompt_tag_key(tag) for tag in source}
    preserved = [tag for tag in source if _prompt_tag_key(tag) not in canonical_keys]
    added = [tag for tag in canonical if _prompt_tag_key(tag) not in source_keys]
    return [*preserved, *canonical], added


def apply_prompt_policy(
    profile: ModelProfile,
    *,
    prompt: Any,
    negative_prompt: Any = "",
) -> dict[str, Any]:
    """선택 모델에 맞는 신뢰 정책을 적용하고 결과 계약용 값을 반환한다."""
    original = _required_text(prompt, "프롬프트", max_length=MAX_PROMPT_LENGTH)
    if (
        not isinstance(negative_prompt, str)
        or len(negative_prompt) > MAX_PROMPT_LENGTH
        or "\x00" in negative_prompt
    ):
        raise WorkflowValidationError("부정 프롬프트 형식이 올바르지 않습니다.")
    original_negative = negative_prompt.strip()
    match = prompt_policy_match_for_profile(profile)
    if match is None:
        return {
            "originalPrompt": original,
            "effectivePrompt": original,
            "effectiveNegativePrompt": original_negative,
            "promptPolicy": {
                "id": "none",
                "label": "모델 기본 프롬프트",
                "description": (
                    "프로필의 architecture와 공식 SHA-256이 Animagine XL 4.0/Opt "
                    "명시적 식별 조건을 충족하지 않아 모델별 자동 태그를 적용하지 않았습니다."
                ),
                "addedPositive": [],
                "addedNegative": [],
            },
        }

    positive = _split_unique_prompt_tags(original)
    if not positive:
        raise WorkflowValidationError("Animagine positive prompt에 유효한 태그가 없습니다.")
    # 공식 권장 주체 태그가 이미 있다면 의미를 바꾸지 않고 맨 앞으로 이동한다.
    subject_index = next(
        (
            index
            for index, tag in enumerate(positive)
            if re.fullmatch(r"[1-9][0-9]*(?:girls?|boys?|others?)", _prompt_tag_key(tag))
        ),
        None,
    )
    if subject_index not in (None, 0):
        positive.insert(0, positive.pop(subject_index))
    effective_positive, added_positive = _canonical_suffix(
        positive, ANIMAGINE_XL_4_POSITIVE_TAGS
    )

    negative = _split_unique_prompt_tags(original_negative)
    effective_negative, added_negative = _canonical_suffix(
        negative, ANIMAGINE_XL_4_NEGATIVE_TAGS
    )
    effective_prompt = ", ".join(effective_positive)
    effective_negative_prompt = ", ".join(effective_negative)
    if len(effective_prompt) > MAX_PROMPT_LENGTH or len(effective_negative_prompt) > MAX_PROMPT_LENGTH:
        raise WorkflowValidationError(
            "Animagine 공식 태그 적용 후 프롬프트가 4,000자 제한을 초과했습니다."
        )
    return {
        "originalPrompt": original,
        "effectivePrompt": effective_prompt,
        "effectiveNegativePrompt": effective_negative_prompt,
        "promptPolicy": {
            "id": match.id,
            "label": match.label,
            "description": match.description,
            "addedPositive": added_positive,
            "addedNegative": added_negative,
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
    if resolved_sampler not in TRUSTED_SAMPLERS:
        raise WorkflowValidationError("지원하지 않는 sampler입니다.")
    if resolved_scheduler not in TRUSTED_SCHEDULERS:
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
    if (
        not isinstance(definition, Sequence)
        or isinstance(definition, (str, bytes))
        or not definition
        or not isinstance(definition[0], Sequence)
        or isinstance(definition[0], (str, bytes))
    ):
        raise WorkflowValidationError(f"ComfyUI 노드 선택값 계약이 다릅니다: {node_class}.{input_name}")
    values = definition[0]
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


def validate_runtime_options(
    profile: ModelProfile,
    options: GenerationOptions,
    node_infos: Mapping[str, Mapping[str, Any]],
) -> None:
    """서버가 실제로 노출하는 모델·sampler·scheduler 선택값과 대조한다."""
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
    else:
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
    workflow = (
        _build_sd_workflow(profile, options, filename_prefix)
        if profile.architecture in (ARCH_SD15, ARCH_SDXL)
        else _build_flux_workflow(profile, options, filename_prefix)
    )
    allowed = set(node_contracts_for_architecture(profile.architecture))
    for node_id, node in workflow.items():
        if not _SAFE_NODE_ID_RE.fullmatch(node_id) or node.get("class_type") not in allowed:
            raise WorkflowValidationError("신뢰되지 않은 ComfyUI 워크플로 노드입니다.")
    return workflow


def _snapshot_input_value(value: Any, node_ids: frozenset[str]) -> Any:
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
        if (
            len(value) != 2
            or not isinstance(value[0], str)
            or value[0] not in node_ids
            or not isinstance(value[1], int)
            or isinstance(value[1], bool)
            or not 0 <= value[1] <= 32
        ):
            raise WorkflowValidationError("워크플로 노드 연결 형식이 올바르지 않습니다.")
        return [value[0], value[1]]
    raise WorkflowValidationError("워크플로 입력 형식이 올바르지 않습니다.")


def snapshot_workflow(
    workflow: Mapping[str, Mapping[str, Any]],
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

    contracts = {**SD_NODE_CONTRACTS, **FLUX_NODE_CONTRACTS}
    asset_inputs = {"ckpt_name", "unet_name", "clip_name1", "clip_name2", "vae_name"}
    snapshot: dict[str, dict[str, Any]] = {}
    for node_id, raw_node in workflow.items():
        if not isinstance(raw_node, Mapping) or set(raw_node) != {"class_type", "inputs"}:
            raise WorkflowValidationError("워크플로 snapshot 노드 형식이 올바르지 않습니다.")
        class_type = raw_node.get("class_type")
        inputs = raw_node.get("inputs")
        contract = contracts.get(class_type) if isinstance(class_type, str) else None
        if contract is None or not isinstance(inputs, Mapping):
            raise WorkflowValidationError("신뢰되지 않은 워크플로 snapshot 노드입니다.")
        if set(inputs) != set(contract.required_inputs):
            raise WorkflowValidationError("워크플로 snapshot 입력 계약이 올바르지 않습니다.")
        copied_inputs: dict[str, Any] = {}
        for input_name, value in inputs.items():
            if input_name in asset_inputs:
                _safe_relative_name(value, f"{class_type}.{input_name}")
            elif input_name == "filename_prefix":
                if not isinstance(value, str) or not re.fullmatch(
                    r"Aiso/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    value,
                ):
                    raise WorkflowValidationError("출력 파일 prefix가 올바르지 않습니다.")
            copied_inputs[input_name] = _snapshot_input_value(value, node_ids)
        snapshot[node_id] = {"class_type": class_type, "inputs": copied_inputs}
    return snapshot


def primary_model_name(profile: ModelProfile) -> str:
    assets = required_assets(profile)
    key = "checkpoint" if profile.architecture in (ARCH_SD15, ARCH_SDXL) else "diffusion_model"
    return assets[key].comfy_name


__all__ = [
    "ANIMAGINE_XL_4_NEGATIVE_TAGS",
    "ANIMAGINE_XL_4_POLICY_ID",
    "ANIMAGINE_XL_4_POLICY_LABEL",
    "ANIMAGINE_XL_4_POSITIVE_TAGS",
    "ARCH_FLUX1_SPLIT",
    "ARCH_SD15",
    "ARCH_SDXL",
    "CAPABILITY_TEXT_TO_IMAGE",
    "FLUX_NODE_CONTRACTS",
    "GenerationOptions",
    "ModelAsset",
    "ModelProfile",
    "PromptPolicyMatch",
    "SD_NODE_CONTRACTS",
    "SUPPORTED_ARCHITECTURES",
    "WorkflowValidationError",
    "apply_prompt_policy",
    "build_workflow",
    "inventory_folders",
    "node_contracts_for_architecture",
    "normalize_asset",
    "normalize_profile",
    "normalize_profiles",
    "primary_model_name",
    "profile_is_installed",
    "prompt_policy_hint_for_profile",
    "prompt_policy_hint_for_raw_profile",
    "prompt_policy_match_for_profile",
    "required_assets",
    "resolve_generation_options",
    "select_profile",
    "snapshot_workflow",
    "validate_node_contracts",
    "validate_runtime_options",
]
