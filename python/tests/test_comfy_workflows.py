# -*- coding: utf-8 -*-
"""사용자 모델 프로필 선택과 신뢰된 ComfyUI 워크플로 단위 테스트."""

from __future__ import annotations

import copy
import hashlib
import json
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


def flux2_klein_profile() -> dict:
    return {
        "id": "profile_flux2_klein",
        "name": "FLUX.2 Klein 4B",
        "family": "flux2",
        "capabilities": ["txt2img"],
        "tags": ["general-purpose", "illustration", "character"],
        "assets": [
            asset("diffusion_model", "diffusion_model", "flux-2-klein-4b-fp8.safetensors"),
            asset("text_encoder", "qwen3", "qwen_3_4b.safetensors"),
            asset("vae", "vae", "flux2-vae.safetensors"),
        ],
        "workflowTemplateId": "flux2.txt2img.v1",
        "defaults": {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0, "sampler": "euler"},
        "agentEnabled": True,
        "priority": 5,
        "createdAt": 1,
        "updatedAt": 1,
    }


def user_api_profile() -> dict:
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "anime-xl.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "AISO_PROMPT", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "AISO_NEGATIVE_PROMPT", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 1024, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "seed": 1, "steps": 28, "cfg": 5.5,
                "sampler_name": "dpmpp_2m", "scheduler": "karras",
                "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0], "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "ComfyUI"}},
    }
    bindings = {
        "positivePrompt": [{"nodeId": "2", "input": "text"}],
        "negativePrompt": [{"nodeId": "3", "input": "text"}],
        "seed": [{"nodeId": "5", "input": "seed"}],
        "width": [{"nodeId": "4", "input": "width"}],
        "height": [{"nodeId": "4", "input": "height"}],
        "steps": [{"nodeId": "5", "input": "steps"}],
        "cfg": [{"nodeId": "5", "input": "cfg"}],
        "sampler": [{"nodeId": "5", "input": "sampler_name"}],
        "scheduler": [{"nodeId": "5", "input": "scheduler"}],
        "filenamePrefix": [{"nodeId": "7", "input": "filename_prefix"}],
    }
    asset_bindings = [{
        "nodeId": "1",
        "input": "ckpt_name",
        "assetId": "custom_asset",
        "sha256": "a" * 64,
        "relativePath": "checkpoints/anime-xl.safetensors",
        "comfyName": "anime-xl.safetensors",
    }]
    encoded = json.dumps(
        {"graph": graph, "bindings": bindings, "assetBindings": asset_bindings}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    template_id = f"user.{digest[:20]}.txt2img.v1"
    return {
        "id": "profile_user_api",
        "name": "Future architecture",
        "family": "custom",
        "capabilities": ["txt2img"],
        "tags": ["future"],
        "assets": [{
            "id": "custom_asset",
            "kind": "custom",
            "fileName": "anime-xl.safetensors",
            "comfyName": "anime-xl.safetensors",
            "relativePath": "checkpoints/anime-xl.safetensors",
            "size": 6_000_000_000,
            "sha256": "a" * 64,
            "importedAt": 1,
        }],
        "workflowTemplateId": template_id,
        "workflowTemplate": {
            "schemaVersion": 1,
            "id": template_id,
            "sourceFileName": "future-api.json",
            "sha256": digest,
            "graph": graph,
            "bindings": bindings,
            "assetBindings": asset_bindings,
            "importedAt": 1,
        },
        "defaults": {
            "width": 768, "height": 1024, "steps": 28, "cfg": 5.5,
            "sampler": "dpmpp_2m", "scheduler": "karras",
        },
        "agentEnabled": True,
        "priority": 7,
        "createdAt": 1,
        "updatedAt": 1,
    }


def rehash_user_template(raw: dict) -> None:
    template = raw["workflowTemplate"]
    digest = hashlib.sha256(json.dumps(
        {
            "graph": template["graph"],
            "bindings": template["bindings"],
            "assetBindings": template["assetBindings"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    template["sha256"] = digest
    template["id"] = f"user.{digest[:20]}.txt2img.v1"
    raw["workflowTemplateId"] = template["id"]


def inventory_for(profiles: list[cw.ModelProfile]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for profile in profiles:
        for item in cw.required_assets(profile).values():
            result.setdefault(item.folder, []).append(item.comfy_name)
    return result


def node_infos(profile: cw.ModelProfile) -> dict[str, dict]:
    infos = {}
    required = cw.required_assets(profile)
    for node_class, contract in cw.node_contracts_for_architecture(profile.architecture).items():
        node_required = {name: ["MODEL", {}] for name in contract.required_inputs}
        infos[node_class] = {
            "name": node_class,
            "python_module": contract.module,
            "input": {"required": node_required},
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
    elif profile.architecture == cw.ARCH_FLUX1_SPLIT:
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
        infos["KSamplerSelect"]["input"]["required"]["sampler_name"] = ["COMBO", {"options": ["euler"]}]
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
    else:
        infos["UNETLoader"]["input"]["required"].update(
            {
                "unet_name": [[required["diffusion_model"].comfy_name], {}],
                "weight_dtype": [["default", "fp8_e4m3fn"], {}],
            }
        )
        infos["CLIPLoader"]["input"]["required"].update(
            {
                "clip_name": [[required["qwen3"].comfy_name], {}],
                "type": [["flux2", "stable_diffusion"], {}],
            }
        )
        infos["VAELoader"]["input"]["required"]["vae_name"] = [[required["vae"].comfy_name], {}]
        infos["KSamplerSelect"]["input"]["required"]["sampler_name"] = ["COMBO", {"options": ["euler"]}]
        infos["Flux2Scheduler"]["input"]["required"].update(
            {
                "steps": ["INT", {"min": 1, "max": 4096}],
                "width": ["INT", {"min": 16, "max": 16384}],
                "height": ["INT", {"min": 16, "max": 16384}],
            }
        )
        infos["EmptyFlux2LatentImage"]["input"]["required"].update(
            {
                "width": ["INT", {"min": 16, "max": 16384}],
                "height": ["INT", {"min": 16, "max": 16384}],
            }
        )
        infos["RandomNoise"]["input"]["required"]["noise_seed"] = ["INT", {"min": 0, "max": cw.MAX_SEED}]
        infos["CFGGuider"]["input"]["required"]["cfg"] = ["FLOAT", {"min": 0.0, "max": 100.0}]
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


def test_manual_profile_selection_is_exact_and_never_falls_back_to_another_model():
    anime = cw.normalize_profile(sd_profile(ident="anime", name="Anime", tags=["anime"], priority=1))
    texture = cw.normalize_profile(
        sd_profile(
            ident="texture",
            name="Texture",
            tags=["texture"],
            priority=50,
            checkpoint="texture.safetensors",
        )
    )
    inventory = inventory_for([anime, texture])

    # The prompt and LLM-supplied hint point to Texture, but the user's exact
    # profile ID wins in manual mode.
    selected, reason = cw.select_profile(
        [anime, texture],
        inventory,
        prompt="photorealistic texture",
        model_hint="Texture",
        selected_profile_id="anime",
    )
    assert selected.id == "anime"
    assert "수동" in reason

    # A stale or unavailable selection is an error, not an automatic fallback.
    with pytest.raises(cw.WorkflowValidationError, match="수동으로 선택한 모델"):
        cw.select_profile(
            [anime, texture],
            {"checkpoints": ["texture.safetensors"]},
            prompt="anime",
            selected_profile_id="anime",
        )
    with pytest.raises(cw.WorkflowValidationError, match="수동 선택 모델 ID"):
        cw.select_profile(
            [anime, texture],
            inventory,
            prompt="anime",
            selected_profile_id="../anime",
        )


def test_manual_profile_normalization_allows_auto_disabled_but_valid_registered_model():
    raw = sd_profile(ident="manual_only", enabled=False)
    with pytest.raises(cw.WorkflowValidationError, match="Agent 자동 선택"):
        cw.normalize_profile(raw)

    manual_only = cw.normalize_profile(raw, require_agent_enabled=False)
    assert manual_only.id == "manual_only"
    assert manual_only.agent_enabled is False
    selected, reason = cw.select_profile(
        [manual_only],
        inventory_for([manual_only]),
        prompt="anime",
        selected_profile_id="manual_only",
    )
    assert selected.id == "manual_only"
    assert "수동" in reason


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


def test_flux2_klein_workflow_uses_qwen3_flux2_scheduler_and_official_zero_negative():
    profile = cw.normalize_profile(flux2_klein_profile())
    assert profile.architecture == cw.ARCH_FLUX2_KLEIN_4B
    assert set(cw.required_assets(profile)) == {"diffusion_model", "qwen3", "vae"}
    options = cw.resolve_generation_options(profile, prompt="navy futuristic jacket", negative_prompt="ignored", seed=7)
    assert (options.width, options.height, options.steps, options.cfg, options.sampler) == (1024, 1024, 4, 1.0, "euler")
    cw.validate_runtime_options(profile, options, node_infos(profile))
    workflow = cw.build_workflow(profile, options, prompt_id="33333333-3333-4333-8333-333333333333")
    assert workflow["2"]["inputs"] == {"clip_name": "qwen_3_4b.safetensors", "type": "flux2"}
    assert workflow["5"]["class_type"] == "ConditioningZeroOut"
    assert workflow["6"]["inputs"]["negative"] == ["5", 0]
    assert workflow["9"]["inputs"] == {"steps": 4, "width": 1024, "height": 1024}
    assert "ignored" not in repr(workflow)
    assert {node["class_type"] for node in workflow.values()} <= set(cw.FLUX2_KLEIN_NODE_CONTRACTS)


def test_user_api_workflow_enables_unknown_model_and_injects_only_bound_generation_values():
    profile = cw.normalize_profile(user_api_profile())
    assert profile.architecture == cw.ARCH_USER_API
    assert cw.required_assets(profile) == {}
    options = cw.resolve_generation_options(
        profile, prompt="rainy future city", negative_prompt="cartoon", seed=99,
        width=1024, height=768,
    )
    infos = node_infos(cw.normalize_profile(sd_profile()))
    cw.validate_runtime_options(profile, options, infos)
    workflow = cw.build_workflow(
        profile, options, prompt_id="44444444-4444-4444-8444-444444444444"
    )
    assert workflow["2"]["inputs"]["text"] == "rainy future city"
    assert workflow["3"]["inputs"]["text"] == "cartoon"
    assert workflow["4"]["inputs"]["width"] == 1024
    assert workflow["4"]["inputs"]["height"] == 768
    assert workflow["5"]["inputs"]["seed"] == 99
    assert workflow["7"]["inputs"]["filename_prefix"].startswith("Aiso/")
    assert cw.snapshot_workflow(workflow, allow_user_template=True) == workflow


def test_user_template_snapshot_allows_live_optional_core_node_inputs():
    """The runtime node schema, not Aiso's narrow built-in template, owns optional inputs."""
    profile = cw.normalize_profile(sd_profile())
    options = cw.resolve_generation_options(profile, prompt="x", seed=1)
    workflow = cw.build_workflow(
        profile, options, prompt_id="45454545-4545-4545-8454-454545454545"
    )
    workflow["5"]["inputs"]["optional_knob"] = 0.5

    snapshot = cw.snapshot_workflow(workflow, allow_user_template=True)
    assert snapshot["5"]["inputs"]["optional_knob"] == 0.5


def test_user_api_workflow_rejects_changed_hash_and_non_core_runtime_node():
    changed = user_api_profile()
    changed["workflowTemplate"]["graph"]["5"]["inputs"]["steps"] = 60
    with pytest.raises(cw.WorkflowValidationError, match="해시"):
        cw.normalize_profile(changed)

    profile = cw.normalize_profile(user_api_profile())
    options = cw.resolve_generation_options(profile, prompt="x", seed=1)
    infos = node_infos(cw.normalize_profile(sd_profile()))
    infos["KSampler"]["python_module"] = "custom_nodes.remote_runner"
    with pytest.raises(cw.WorkflowValidationError, match="기본 노드"):
        cw.validate_runtime_options(profile, options, infos)


def test_user_api_workflow_rejects_a_loader_bound_to_a_different_registered_asset():
    broken = user_api_profile()
    broken["workflowTemplate"]["assetBindings"][0]["sha256"] = "b" * 64
    rehash_user_template(broken)
    with pytest.raises(cw.WorkflowValidationError, match="등록 자산"):
        cw.normalize_profile(broken)

    wrong_name = user_api_profile()
    wrong_name["workflowTemplate"]["graph"]["1"]["inputs"]["ckpt_name"] = "other.safetensors"
    wrong_name["workflowTemplate"]["assetBindings"][0]["comfyName"] = "other.safetensors"
    rehash_user_template(wrong_name)
    with pytest.raises(cw.WorkflowValidationError, match="등록 자산"):
        cw.normalize_profile(wrong_name)


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


def test_prompt_policy_applies_sd_negative_and_flux_positive_constraints():
    prompt = "blue hair, masterpiece, 1girl, blue hair, cherry blossoms"
    negative_prompt = "custom artifact, BAD HANDS, bad hands"
    sdxl = cw.normalize_profile(
        sd_profile(name="User-supplied SDXL", tags=["anime", "character"])
    )
    flux = cw.normalize_profile(flux_profile())

    sd_applied = cw.apply_prompt_policy(sdxl, prompt=prompt, negative_prompt=negative_prompt)
    assert sd_applied["originalPrompt"] == prompt
    assert sd_applied["originalNegativePrompt"] == negative_prompt
    assert sd_applied["effectivePrompt"] == prompt
    assert sd_applied["promptPolicy"]["id"] == "sd-negative-quality-v1"
    assert sd_applied["promptPolicy"]["addedPositive"] == []
    assert "low quality" in sd_applied["promptPolicy"]["addedNegative"]
    assert "bad anatomy" in sd_applied["promptPolicy"]["addedNegative"]
    assert sd_applied["effectiveNegativePrompt"].startswith(negative_prompt)

    flux_applied = cw.apply_prompt_policy(flux, prompt=prompt, negative_prompt=negative_prompt)
    assert flux_applied["originalPrompt"] == prompt
    assert flux_applied["originalNegativePrompt"] == negative_prompt
    assert flux_applied["effectiveNegativePrompt"] == ""
    assert flux_applied["promptPolicy"]["id"] == "flux-positive-constraints-v1"
    assert flux_applied["promptPolicy"]["addedNegative"] == []
    assert flux_applied["effectivePrompt"].startswith(prompt)
    assert "natural hands" in flux_applied["effectivePrompt"]


def test_prompt_policy_preserves_deliberate_blur_low_fidelity_and_distortion():
    sdxl = cw.normalize_profile(sd_profile(name="General SDXL", tags=["general"]))
    prompt = (
        "pixel art handbag on a personal desk, intentional motion blur, JPEG aesthetic, "
        "body horror creature with extra arms"
    )
    applied = cw.apply_prompt_policy(
        sdxl,
        prompt=prompt,
        negative_prompt="watermark",
    )

    additions = applied["promptPolicy"]["addedNegative"]
    assert "low quality" not in additions
    assert "low resolution" not in additions
    assert "blurry" not in additions
    assert "jpeg artifacts" not in additions
    assert "bad anatomy" not in additions
    assert "malformed hands" not in additions

    plain_object = cw.apply_prompt_policy(
        sdxl,
        prompt="a handbag on a personal desk",
        negative_prompt="watermark",
    )
    # Whole-token matching prevents handbag/personal from becoming hand/man.
    assert "bad anatomy" not in plain_object["promptPolicy"]["addedNegative"]
    assert "malformed hands" not in plain_object["promptPolicy"]["addedNegative"]

    direct_blur = cw.apply_prompt_policy(
        sdxl,
        prompt="a blurry portrait in an out of focus photograph",
        negative_prompt="watermark",
    )
    assert "blurry" not in direct_blur["promptPolicy"]["addedNegative"]

    flux = cw.normalize_profile(flux_profile())
    flux_blur = cw.apply_prompt_policy(
        flux,
        prompt="a deliberately blurred scene, out of focus photograph",
        negative_prompt="blurry",
    )
    assert flux_blur["effectivePrompt"] == "a deliberately blurred scene, out of focus photograph"
    assert flux_blur["promptPolicy"]["id"] == "flux-negative-unconnected-v1"


def test_flux_without_convertible_negative_does_not_claim_a_conversion():
    flux = cw.normalize_profile(flux_profile())
    applied = cw.apply_prompt_policy(
        flux,
        prompt="abstract glitch art with chaotic distortion",
        negative_prompt="custom private concept",
    )

    assert applied["effectivePrompt"] == "abstract glitch art with chaotic distortion"
    assert applied["effectiveNegativePrompt"] == ""
    assert applied["promptPolicy"]["id"] == "flux-negative-unconnected-v1"
    assert applied["promptPolicy"]["addedPositive"] == []


def test_user_workflow_without_negative_binding_reports_it_as_unconnected():
    from dataclasses import replace

    profile = cw.normalize_profile(user_api_profile())
    assert profile.workflow_template is not None
    bindings = dict(profile.workflow_template.bindings)
    bindings["negativePrompt"] = ()
    profile = replace(
        profile,
        workflow_template=replace(profile.workflow_template, bindings=bindings),
    )
    applied = cw.apply_prompt_policy(
        profile,
        prompt="an original character",
        negative_prompt="watermark",
    )

    assert applied["effectivePrompt"] == "an original character"
    assert applied["effectiveNegativePrompt"] == ""
    assert applied["promptPolicy"]["id"] == "user-workflow-negative-unbound-v1"


def test_profile_tag_matching_uses_phrase_boundaries_not_substrings():
    profiles = cw.normalize_profiles([
        sd_profile(ident="art", name="Art", tags=["art"], priority=0, checkpoint="art.safetensors"),
        sd_profile(ident="fallback", name="Fallback", tags=["general"], priority=1, checkpoint="fallback.safetensors"),
    ])
    inventory = {"checkpoints": ["art.safetensors", "fallback.safetensors"]}
    selected, _ = cw.select_profile(profiles, inventory, prompt="a cartoon character")
    assert selected.id == "fallback"


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
