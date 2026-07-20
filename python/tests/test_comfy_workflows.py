# -*- coding: utf-8 -*-
"""사용자 모델 프로필 선택과 신뢰된 ComfyUI 워크플로 단위 테스트."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comfy_workflows as cw  # noqa: E402


def asset(kind: str, slot: str, name: str, ident: str | None = None) -> dict:
    folders = {
        "checkpoint": "checkpoints",
        "diffusion_model": "diffusion_models",
        "text_encoder": "text_encoders",
        "vae": "vae",
        "lora": "loras",
        "controlnet": "controlnet",
    }
    return {
        "id": ident or f"asset_{slot}",
        "kind": kind,
        "slot": slot,
        "fileName": name.rsplit("/", 1)[-1],
        "comfyName": name,
        "relativePath": f"{folders[kind]}/{name}",
        "size": 6_000_000_000,
        "sha256": "a" * 64,
        "importedAt": 1_753_000_000_000,
    }


def sd_profile(
    *,
    ident: str = "profile_sdxl",
    name: str = "Anime XL",
    tags: list[str] | None = None,
    priority: int = 10,
    enabled: bool = True,
    checkpoint: str = "anime-xl.safetensors",
    extras: list[dict] | None = None,
) -> dict:
    # 실제 src/shared/comfy-model.ts의 ComfyModelProfile 모양을 그대로 사용한다.
    return {
        "id": ident,
        "name": name,
        "family": "sdxl",
        "capabilities": ["txt2img"],
        "tags": tags or ["anime", "애니메이션"],
        "assets": [asset("checkpoint", "checkpoint", checkpoint), *(extras or [])],
        "workflowTemplateId": "sdxl.txt2img.v1",
        "defaults": {
            "width": 768,
            "height": 1024,
            "steps": 28,
            "cfg": 5.5,
            "sampler": "dpmpp_2m",
            "scheduler": "karras",
        },
        "agentEnabled": enabled,
        "priority": priority,
        "createdAt": 1_753_000_000_000,
        "updatedAt": 1_753_000_000_000,
    }


def flux_profile(*, extras: list[dict] | None = None) -> dict:
    return {
        "id": "profile_flux1",
        "name": "My FLUX.1",
        "family": "flux1",
        "capabilities": ["txt2img"],
        "tags": ["texture", "텍스처"],
        "assets": [
            asset("diffusion_model", "diffusion_model", "flux1-dev.safetensors"),
            asset("text_encoder", "clip_l", "clip_l.safetensors"),
            asset("text_encoder", "t5xxl", "t5xxl_fp8.safetensors"),
            asset("vae", "vae", "ae.safetensors"),
            *(extras or []),
        ],
        "workflowTemplateId": "flux1.txt2img.v1",
        "defaults": {"width": 1024, "height": 1024, "steps": 24, "cfg": 4.25},
        "agentEnabled": True,
        "priority": 5,
        "createdAt": 1,
        "updatedAt": 1,
    }


def inventory_for(profiles: list[cw.ModelProfile]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for profile in profiles:
        for item in cw.required_assets(profile).values():
            result.setdefault(item.folder, []).append(item.comfy_name)
    return result


def node_infos(profile: cw.ModelProfile) -> dict[str, dict]:
    infos = {}
    for node_class, contract in cw.node_contracts_for_architecture(profile.architecture).items():
        required = {name: ["MODEL", {}] for name in contract.required_inputs}
        infos[node_class] = {
            "name": node_class,
            "python_module": contract.module,
            "input": {"required": required},
        }
    if profile.architecture in {cw.ARCH_SD15, cw.ARCH_SDXL}:
        checkpoint = cw.required_assets(profile)["checkpoint"].comfy_name
        infos["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [[checkpoint], {}]
        infos["KSampler"]["input"]["required"].update(
            {
                "sampler_name": [["euler", "dpmpp_2m"], {}],
                "scheduler": [["normal", "karras"], {}],
                "seed": ["INT", {"min": 0, "max": cw.MAX_SEED}],
                "steps": ["INT", {"min": 1, "max": 10000}],
                "cfg": ["FLOAT", {"min": 0.0, "max": 100.0}],
            }
        )
        infos["EmptyLatentImage"]["input"]["required"].update(
            {
                "width": ["INT", {"min": 16, "max": 16384}],
                "height": ["INT", {"min": 16, "max": 16384}],
            }
        )
    else:
        required = cw.required_assets(profile)
        infos["UNETLoader"]["input"]["required"].update(
            {
                "unet_name": [[required["diffusion_model"].comfy_name], {}],
                "weight_dtype": [["default", "fp8_e4m3fn"], {}],
            }
        )
        infos["DualCLIPLoader"]["input"]["required"].update(
            {
                "clip_name1": [[required["clip_l"].comfy_name], {}],
                "clip_name2": [[required["t5xxl"].comfy_name], {}],
                "type": [["flux", "sdxl"], {}],
            }
        )
        infos["VAELoader"]["input"]["required"]["vae_name"] = [[required["vae"].comfy_name], {}]
        infos["KSamplerSelect"]["input"]["required"]["sampler_name"] = [["euler"], {}]
        infos["BasicScheduler"]["input"]["required"].update(
            {
                "scheduler": [["simple", "normal"], {}],
                "steps": ["INT", {"min": 1, "max": 10000}],
            }
        )
        infos["EmptySD3LatentImage"]["input"]["required"].update(
            {
                "width": ["INT", {"min": 16, "max": 16384}],
                "height": ["INT", {"min": 16, "max": 16384}],
            }
        )
        infos["RandomNoise"]["input"]["required"]["noise_seed"] = [
            "INT",
            {"min": 0, "max": cw.MAX_SEED},
        ]
        infos["FluxGuidance"]["input"]["required"]["guidance"] = [
            "FLOAT",
            {"min": 0.0, "max": 100.0},
        ]
    return infos


def test_real_typescript_profile_shape_and_defaults_are_preserved():
    profile = cw.normalize_profile(sd_profile())
    assert profile.architecture == cw.ARCH_SDXL
    assert profile.defaults == cw.ProfileDefaults(768, 1024, 28, 5.5, "dpmpp_2m", "karras")
    assert set(cw.required_assets(profile)) == {"checkpoint"}


def test_legacy_architecture_field_remains_compatible():
    raw = sd_profile()
    raw.pop("family")
    raw["architecture"] = "sdxl"
    assert cw.normalize_profile(raw).architecture == cw.ARCH_SDXL


def test_flux_family_and_typescript_slots_map_to_split_architecture():
    profile = cw.normalize_profile(flux_profile())
    assert profile.architecture == cw.ARCH_FLUX1_SPLIT
    assert set(cw.required_assets(profile)) == {"diffusion_model", "clip_l", "t5xxl", "vae"}


def test_optional_lora_and_controlnet_are_kept_but_not_required_or_reconciled():
    extras = [
        asset("lora", "lora", "style.safetensors", "optional_lora"),
        asset("controlnet", "controlnet", "pose.safetensors", "optional_controlnet"),
    ]
    profile = cw.normalize_profile(sd_profile(extras=extras))
    assert len(profile.assets) == 3
    assert set(cw.required_assets(profile)) == {"checkpoint"}
    assert cw.inventory_folders([profile]) == frozenset({"checkpoints"})
    selected, reason = cw.select_profile(
        [profile], {"checkpoints": ["anime-xl.safetensors"]}, prompt="anime"
    )
    assert selected.id == profile.id
    assert "적용하지 않았습니다" in reason


def test_non_template_vae_is_kept_without_disabling_sdxl_checkpoint_profile():
    profile = cw.normalize_profile(
        sd_profile(extras=[asset("vae", "vae", "manual-vae.safetensors", "manual_vae")])
    )
    assert len(profile.assets) == 2
    assert set(cw.required_assets(profile)) == {"checkpoint"}
    selected, reason = cw.select_profile(
        [profile], {"checkpoints": ["anime-xl.safetensors"]}, prompt="anime"
    )
    assert selected.id == profile.id
    assert "추가 자산 1개" in reason


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(agentEnabled=False),
        lambda p: p.update(family="flux2"),
        lambda p: p.update(capabilities=["img2img"]),
        lambda p: p["assets"][0].update(fileName="unsafe.ckpt", comfyName="unsafe.ckpt"),
        lambda p: p.update(workflowTemplateId="custom.workflow"),
    ],
)
def test_unsupported_profiles_are_not_agent_candidates(mutate):
    profile = sd_profile()
    mutate(profile)
    with pytest.raises(cw.WorkflowValidationError, match="프로필"):
        cw.normalize_profiles([profile])


def test_duplicate_or_incomplete_required_assets_are_rejected():
    duplicate = sd_profile(extras=[asset("checkpoint", "checkpoint", "second.safetensors", "second")])
    with pytest.raises(cw.WorkflowValidationError):
        cw.normalize_profile(duplicate)
    incomplete = flux_profile()
    incomplete["assets"] = incomplete["assets"][:-1]
    with pytest.raises(cw.WorkflowValidationError, match="완전하지"):
        cw.normalize_profile(incomplete)


def test_selection_exact_hint_then_tags_then_priority_is_deterministic():
    anime = cw.normalize_profile(sd_profile(ident="anime", name="Anime", tags=["anime"], priority=1))
    texture = cw.normalize_profile(
        sd_profile(ident="texture", name="Texture", tags=["texture", "텍스처"], priority=50,
                   checkpoint="texture.safetensors")
    )
    profiles = [texture, anime]
    inventory = inventory_for(profiles)
    selected, _ = cw.select_profile(profiles, inventory, prompt="anime character")
    assert selected.id == "anime"
    selected, reason = cw.select_profile(profiles, inventory, prompt="anime character", model_hint="Texture")
    assert selected.id == "texture"
    assert "정확히 일치" in reason
    selected, _ = cw.select_profile(profiles, inventory, prompt="unmatched")
    assert selected.id == "texture"


def test_missing_higher_ranked_model_falls_back_to_installed_profile():
    missing = cw.normalize_profile(sd_profile(ident="missing", priority=100))
    ready = cw.normalize_profile(
        sd_profile(ident="ready", checkpoint="ready.safetensors", priority=1)
    )
    selected, _ = cw.select_profile(
        [missing, ready], {"checkpoints": ["ready.safetensors"]}, prompt="anime"
    )
    assert selected.id == "ready"


def test_sdxl_workflow_uses_profile_defaults_and_trusted_api_nodes_only():
    profile = cw.normalize_profile(sd_profile())
    options = cw.resolve_generation_options(
        profile, prompt="1girl", negative_prompt="bad hands", seed=str(cw.MAX_SEED)
    )
    assert (options.width, options.height, options.steps, options.cfg) == (768, 1024, 28, 5.5)
    assert options.seed == cw.MAX_SEED
    infos = node_infos(profile)
    cw.validate_runtime_options(profile, options, infos)
    workflow = cw.build_workflow(
        profile, options, prompt_id="11111111-1111-4111-8111-111111111111"
    )
    assert workflow["1"]["inputs"]["ckpt_name"] == "anime-xl.safetensors"
    assert workflow["5"]["inputs"]["seed"] == cw.MAX_SEED
    assert workflow["7"]["inputs"]["filename_prefix"].startswith("Aiso/")
    assert {node["class_type"] for node in workflow.values()} <= set(cw.SD_NODE_CONTRACTS)


def test_flux_workflow_is_split_and_does_not_inject_negative_prompt():
    profile = cw.normalize_profile(flux_profile())
    options = cw.resolve_generation_options(
        profile, prompt="fantasy texture", negative_prompt="ignored", seed=7
    )
    assert (options.steps, options.cfg, options.sampler, options.scheduler) == (24, 4.25, "euler", "simple")
    infos = node_infos(profile)
    cw.validate_runtime_options(profile, options, infos)
    workflow = cw.build_workflow(
        profile, options, prompt_id="22222222-2222-4222-8222-222222222222"
    )
    assert workflow["1"]["class_type"] == "UNETLoader"
    assert workflow["2"]["inputs"]["type"] == "flux"
    assert workflow["5"]["inputs"]["guidance"] == 4.25
    assert "ignored" not in repr(workflow)
    assert {node["class_type"] for node in workflow.values()} <= set(cw.FLUX_NODE_CONTRACTS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": " ", "seed": 1},
        {"prompt": "x", "width": 300, "seed": 1},
        {"prompt": "x", "steps": 61, "seed": 1},
        {"prompt": "x", "cfg": float("nan"), "seed": 1},
        {"prompt": "x", "seed": str(cw.MAX_SEED + 1)},
        {"prompt": "x", "seed": "01"},
    ],
)
def test_generation_bounds_reject_unsafe_values(kwargs):
    with pytest.raises(cw.WorkflowValidationError):
        cw.resolve_generation_options(cw.normalize_profile(sd_profile()), **kwargs)


def test_node_contract_rejects_custom_override_missing_input_and_server_seed_limit():
    profile = cw.normalize_profile(sd_profile())
    options = cw.resolve_generation_options(profile, prompt="x", seed=10)
    infos = node_infos(profile)
    infos["KSampler"]["python_module"] = "custom_nodes.evil"
    with pytest.raises(cw.WorkflowValidationError, match="신뢰할 수 없는"):
        cw.validate_runtime_options(profile, options, infos)
    infos = node_infos(profile)
    del infos["KSampler"]["input"]["required"]["negative"]
    with pytest.raises(cw.WorkflowValidationError, match="입력 계약"):
        cw.validate_runtime_options(profile, options, infos)
    infos = node_infos(profile)
    infos["KSampler"]["input"]["required"]["seed"] = ["INT", {"min": 0, "max": 5}]
    with pytest.raises(cw.WorkflowValidationError, match="seed=10"):
        cw.validate_runtime_options(profile, options, infos)


def test_animagine_official_sha_applies_suffixes_without_duplicates():
    raw = sd_profile(
        name="사용자 지정 애니 모델",
        tags=["anime", "animagine-xl-4.0-opt"],
        checkpoint="renamed-user-model.safetensors",
    )
    raw["assets"][0]["sha256"] = (
        "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
    )
    profile = cw.normalize_profile(raw)
    applied = cw.apply_prompt_policy(
        profile,
        prompt="blue hair, masterpiece, 1girl, blue hair, cherry blossoms",
        negative_prompt="custom artifact, BAD HANDS, bad hands",
    )
    assert applied["originalPrompt"] == (
        "blue hair, masterpiece, 1girl, blue hair, cherry blossoms"
    )
    assert applied["effectivePrompt"] == (
        "1girl, blue hair, cherry blossoms, masterpiece, high score, great score, absurdres"
    )
    assert applied["effectiveNegativePrompt"].startswith("custom artifact, lowres, bad anatomy")
    assert applied["effectiveNegativePrompt"].count("bad hands") == 1
    assert applied["promptPolicy"] == {
        "id": cw.ANIMAGINE_XL_4_POLICY_ID,
        "label": cw.ANIMAGINE_XL_4_POLICY_LABEL,
        "description": applied["promptPolicy"]["description"],
        "addedPositive": ["high score", "great score", "absurdres"],
        "addedNegative": [
            tag for tag in cw.ANIMAGINE_XL_4_NEGATIVE_TAGS if tag != "bad hands"
        ],
    }
    assert "SHA-256" in applied["promptPolicy"]["description"]


def test_animagine_file_name_alone_does_not_activate_policy():
    profile = cw.normalize_profile(
        sd_profile(
            name="My General SDXL",
            tags=["anime"],
            checkpoint="animagine-xl-4.0-opt.safetensors",
        )
    )
    assert cw.prompt_policy_match_for_profile(profile) is None
    applied = cw.apply_prompt_policy(profile, prompt="A girl under cherry blossoms")
    assert applied["effectivePrompt"] == "A girl under cherry blossoms"
    assert applied["effectiveNegativePrompt"] == ""
    assert applied["promptPolicy"]["id"] == "none"


def test_animagine_official_sha_is_required_identity_evidence():
    raw = sd_profile(
        name="Renamed verified model",
        tags=["anime"],
        checkpoint="renamed.safetensors",
    )
    raw["assets"][0]["sha256"] = (
        "1d5b43ff75b6ab598502d4c779d2fbfa3dceca51c60c3b609640a60772333916"
    )
    match = cw.prompt_policy_match_for_profile(cw.normalize_profile(raw))
    assert match is not None
    assert match.id == cw.ANIMAGINE_XL_4_POLICY_ID
    assert "SHA-256" in match.description


def test_animagine_canonical_name_and_file_without_official_sha_do_not_activate_policy():
    raw = sd_profile(
        name="Animagine XL 4.0 Opt",
        tags=["animagine-xl-4.0-opt"],
        checkpoint="animagine-xl-4.0-opt.safetensors",
    )
    assert cw.prompt_policy_match_for_profile(cw.normalize_profile(raw)) is None


def test_animagine_policy_is_never_applied_to_flux_or_generic_sdxl():
    raw_flux = flux_profile()
    raw_flux["tags"] = ["animagine-xl-4.0"]
    flux = cw.normalize_profile(raw_flux)
    generic = cw.normalize_profile(sd_profile(name="Generic SDXL", tags=["anime"]))
    tagged_impostor = cw.normalize_profile(
        sd_profile(name="Generic SDXL", tags=["animagine-xl-4.0-opt"])
    )
    assert cw.prompt_policy_match_for_profile(flux) is None
    assert cw.prompt_policy_match_for_profile(generic) is None
    assert cw.prompt_policy_match_for_profile(tagged_impostor) is None
    assert cw.apply_prompt_policy(flux, prompt="texture")["promptPolicy"]["id"] == "none"


def test_raw_profile_policy_hint_guides_llm_to_tag_prompt_without_prose():
    raw = sd_profile(
        name="Animagine XL 4.0 Opt",
        tags=["anime"],
        checkpoint="animagine-xl-4.0-opt.safetensors",
    )
    raw["assets"][0]["sha256"] = (
        "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
    )
    hint = cw.prompt_policy_hint_for_raw_profile(raw)
    assert hint is not None
    assert hint["id"] == cw.ANIMAGINE_XL_4_POLICY_ID
    assert "쉼표 구분 태그" in hint["instructions"]
    assert "1girl" in hint["instructions"] and "1boy" in hint["instructions"]
    assert "산문" in hint["instructions"]
    assert cw.prompt_policy_hint_for_raw_profile(sd_profile(tags=["anime"])) is None
    assert cw.prompt_policy_hint_for_raw_profile(
        sd_profile(name="Generic SDXL", tags=["animagine-xl-4.0-opt"])
    ) is None


def test_workflow_snapshot_is_independent_and_keeps_safe_connections_only():
    profile = cw.normalize_profile(sd_profile())
    options = cw.resolve_generation_options(
        profile, prompt="1girl, blue hair", negative_prompt="bad hands", seed=7
    )
    workflow = cw.build_workflow(
        profile, options, prompt_id="11111111-1111-4111-8111-111111111111"
    )
    snapshot = cw.snapshot_workflow(workflow)
    assert snapshot == workflow
    assert snapshot is not workflow
    assert snapshot["2"]["inputs"]["clip"] == ["1", 1]
    assert set(snapshot["2"]) == {"class_type", "inputs"}
    snapshot["2"]["inputs"]["clip"][1] = 9
    assert workflow["2"]["inputs"]["clip"] == ["1", 1]
    assert "client_id" not in repr(snapshot)
    assert "X-Aiso-Token" not in repr(snapshot)


def test_workflow_snapshot_rejects_absolute_asset_path_and_secret_extra_input():
    profile = cw.normalize_profile(sd_profile())
    options = cw.resolve_generation_options(profile, prompt="1girl", seed=7)
    workflow = cw.build_workflow(
        profile, options, prompt_id="11111111-1111-4111-8111-111111111111"
    )
    absolute = copy.deepcopy(workflow)
    absolute["1"]["inputs"]["ckpt_name"] = "C:\\models\\secret.safetensors"
    with pytest.raises(cw.WorkflowValidationError):
        cw.snapshot_workflow(absolute)
    extra = copy.deepcopy(workflow)
    extra["1"]["inputs"]["api_token"] = "secret"
    with pytest.raises(cw.WorkflowValidationError, match="입력 계약"):
        cw.snapshot_workflow(extra)
