# -*- coding: utf-8 -*-
"""모델 자동 선택부터 ComfyUI jobs poll/output ref까지의 생성 서비스 테스트."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comfy_client  # noqa: E402
import comfy_generation as generation  # noqa: E402
import comfy_workflows as cw  # noqa: E402


def raw_profile(
    *,
    ident: str = "anime_model",
    name: str = "User Anime XL",
    tag: str = "anime",
    priority: int = 10,
    checkpoint: str = "user-anime.safetensors",
    quality_mode: str | None = None,
) -> dict:
    profile = {
        "id": ident,
        "name": name,
        "family": "sdxl",
        "capabilities": ["txt2img"],
        "tags": [tag],
        "assets": [
            {
                "id": f"{ident}_checkpoint",
                "kind": "checkpoint",
                "slot": "checkpoint",
                "fileName": checkpoint,
                "comfyName": checkpoint,
                "relativePath": f"checkpoints/{checkpoint}",
                "size": 7_000_000_000,
                "sha256": "b" * 64,
                "importedAt": 1,
            }
        ],
        "workflowTemplateId": "sdxl.txt2img.v1",
        "defaults": {
            "width": 832,
            "height": 1216,
            "steps": 30,
            "cfg": 6.0,
            "sampler": "euler",
            "scheduler": "normal",
        },
        "agentEnabled": True,
        "priority": priority,
        "createdAt": 1,
        "updatedAt": 1,
    }
    if quality_mode is not None:
        profile["qualityMode"] = quality_mode
    return profile


def raw_flux2_klein_profile() -> dict:
    return {
        "id": "flux2_klein",
        "name": "FLUX.2 Klein 4B",
        "family": "flux2",
        "capabilities": ["txt2img"],
        "tags": ["illustration", "character", "general-purpose"],
        "assets": [
            {
                "id": "flux2_model",
                "kind": "diffusion_model",
                "slot": "diffusion_model",
                "fileName": "flux-2-klein-4b-fp8.safetensors",
                "comfyName": "flux-2-klein-4b-fp8.safetensors",
                "relativePath": "diffusion_models/flux-2-klein-4b-fp8.safetensors",
                "size": 4_000_000_000,
                "sha256": "c" * 64,
                "importedAt": 1,
            },
            {
                "id": "flux2_qwen",
                "kind": "text_encoder",
                "slot": "qwen3",
                "fileName": "qwen_3_4b.safetensors",
                "comfyName": "qwen_3_4b.safetensors",
                "relativePath": "text_encoders/qwen_3_4b.safetensors",
                "size": 8_000_000_000,
                "sha256": "d" * 64,
                "importedAt": 1,
            },
            {
                "id": "flux2_vae",
                "kind": "vae",
                "slot": "vae",
                "fileName": "flux2-vae.safetensors",
                "comfyName": "flux2-vae.safetensors",
                "relativePath": "vae/flux2-vae.safetensors",
                "size": 336_000_000,
                "sha256": "e" * 64,
                "importedAt": 1,
            },
        ],
        "workflowTemplateId": "flux2.txt2img.v1",
        "defaults": {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0, "sampler": "euler"},
        "agentEnabled": True,
        "priority": 10,
        "createdAt": 1,
        "updatedAt": 1,
    }


def raw_user_workflow_profile() -> dict:
    raw = raw_profile(ident="user_workflow", name="User API Workflow")
    base = cw.normalize_profile(raw)
    options = cw.resolve_generation_options(
        base, prompt="template prompt", negative_prompt="template negative", seed=1
    )
    graph = cw.build_workflow(
        base, options, prompt_id="55555555-5555-4555-8555-555555555555"
    )
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
    asset = raw["assets"][0]
    asset_bindings = [{
        "nodeId": "1",
        "input": "ckpt_name",
        "assetId": asset["id"],
        "sha256": asset["sha256"],
        "relativePath": asset["relativePath"],
        "comfyName": graph["1"]["inputs"]["ckpt_name"],
    }]
    digest = hashlib.sha256(json.dumps(
        {"graph": graph, "bindings": bindings, "assetBindings": asset_bindings}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    template_id = f"user.{digest[:20]}.txt2img.v1"
    raw["workflowTemplateId"] = template_id
    raw["workflowTemplate"] = {
        "schemaVersion": 1,
        "id": template_id,
        "sourceFileName": "user-api.json",
        "sha256": digest,
        "graph": graph,
        "bindings": bindings,
        "assetBindings": asset_bindings,
        "importedAt": 1,
    }
    return raw


def sd_node_info(profile: cw.ModelProfile, node_class: str) -> dict:
    contract = cw.node_contracts_for_architecture(profile.architecture)[node_class]
    required = {name: ["MODEL", {}] for name in contract.required_inputs}
    info = {"name": node_class, "python_module": contract.module, "input": {"required": required}}
    if node_class == "CheckpointLoaderSimple":
        required["ckpt_name"] = [[cw.primary_model_name(profile)], {}]
    elif node_class == "KSampler":
        required.update(
            {
                "sampler_name": [["euler"], {}],
                "scheduler": [["normal"], {}],
                "seed": ["INT", {"min": 0, "max": cw.MAX_SEED}],
                "steps": ["INT", {"min": 1, "max": 10000}],
                "cfg": ["FLOAT", {"min": 0, "max": 100}],
            }
        )
    elif node_class == "EmptyLatentImage":
        required.update(
            {
                "width": ["INT", {"min": 16, "max": 16384}],
                "height": ["INT", {"min": 16, "max": 16384}],
            }
        )
    return info


def latent_upscale_node_info() -> dict:
    return {
        "name": "LatentUpscale",
        "python_module": "nodes",
        "input": {
            "required": {
                "samples": ["LATENT", {}],
                "upscale_method": [["bislerp"], {}],
                "width": ["INT", {"min": 64, "max": 2048}],
                "height": ["INT", {"min": 64, "max": 2048}],
                "crop": [["disabled"], {}],
            }
        },
    }


def flux2_node_info(profile: cw.ModelProfile, node_class: str) -> dict:
    contract = cw.node_contracts_for_architecture(profile.architecture)[node_class]
    required = {name: ["MODEL", {}] for name in contract.required_inputs}
    info = {"name": node_class, "python_module": contract.module, "input": {"required": required}}
    assets = cw.required_assets(profile)
    if node_class == "UNETLoader":
        required.update({
            "unet_name": [[assets["diffusion_model"].comfy_name], {}],
            "weight_dtype": [["default"], {}],
        })
    elif node_class == "CLIPLoader":
        required.update({"clip_name": [[assets["qwen3"].comfy_name], {}], "type": [["flux2"], {}]})
    elif node_class == "VAELoader":
        required["vae_name"] = [[assets["vae"].comfy_name], {}]
    elif node_class == "KSamplerSelect":
        required["sampler_name"] = ["COMBO", {"options": ["euler"]}]
    elif node_class == "Flux2Scheduler":
        required.update({
            "steps": ["INT", {"min": 1, "max": 4096}],
            "width": ["INT", {"min": 16, "max": 16384}],
            "height": ["INT", {"min": 16, "max": 16384}],
        })
    elif node_class == "EmptyFlux2LatentImage":
        required.update({
            "width": ["INT", {"min": 16, "max": 16384}],
            "height": ["INT", {"min": 16, "max": 16384}],
        })
    elif node_class == "RandomNoise":
        required["noise_seed"] = ["INT", {"min": 0, "max": cw.MAX_SEED}]
    elif node_class == "CFGGuider":
        required["cfg"] = ["FLOAT", {"min": 0.0, "max": 100.0}]
    return info


def install_success_fakes(
    monkeypatch,
    profiles: list[dict],
    *,
    seed_output: int = 42,
    allow_manual_only_profiles: bool = False,
) -> dict:
    normalized = [
        cw.normalize_profile(profile, require_agent_enabled=not allow_manual_only_profiles)
        for profile in profiles
    ]
    inventory = {
        "checkpoints": [cw.primary_model_name(profile) for profile in normalized],
    }
    captured: dict = {"order": []}

    async def capability(base_url):
        captured["order"].append("capability")
        return {"supported": True, "baseUrl": base_url}

    async def get_inventory(base_url, folders):
        captured["order"].append("inventory")
        captured["base_url"] = base_url
        assert folders == frozenset({"checkpoints"})
        return inventory

    async def get_node_info(_base_url, node_class):
        if node_class == "LatentUpscale":
            return latent_upscale_node_info()
        info = sd_node_info(normalized[0], node_class)
        if node_class == "CheckpointLoaderSimple":
            info["input"]["required"]["ckpt_name"] = [
                [cw.primary_model_name(profile) for profile in normalized],
                {},
            ]
        return info

    async def submit(_base_url, workflow, *, client_id, prompt_id):
        captured["order"].append("submit")
        captured["workflow"] = workflow
        captured["client_id"] = client_id
        captured["prompt_id"] = prompt_id
        captured["selected_model"] = workflow["1"]["inputs"]["ckpt_name"]
        return {"promptId": prompt_id, "queueNumber": 1, "nodeErrors": {}}

    async def get_job(_base_url, prompt_id):
        captured["order"].append("poll")
        return {
            "promptId": prompt_id,
            "status": "completed",
            "terminal": True,
            "outputs": [
                {
                    "nodeId": "7",
                    "index": 0,
                    "filename": "result_00001_.png",
                    "subfolder": "Aiso",
                    "storageType": "output",
                }
            ],
            "error": None,
        }

    async def release_models(_base_url):
        captured["order"].append("release")
        return {"requested": True}

    monkeypatch.setattr(comfy_client, "get_jobs_capability", capability)
    monkeypatch.setattr(comfy_client, "get_models_inventory", get_inventory)
    monkeypatch.setattr(comfy_client, "get_node_info", get_node_info)
    monkeypatch.setattr(comfy_client, "submit_prompt", submit)
    monkeypatch.setattr(comfy_client, "get_job", get_job)
    monkeypatch.setattr(comfy_client, "release_models", release_models)
    monkeypatch.setattr(generation.secrets, "randbelow", lambda _limit: seed_output)
    return captured


def install_flux2_success_fakes(monkeypatch, profile: dict) -> dict:
    normalized = cw.normalize_profile(profile)
    captured: dict = {}

    async def capability(_base_url):
        return {"supported": True}

    async def get_inventory(_base_url, folders):
        assert folders == frozenset({"diffusion_models", "text_encoders", "vae"})
        return {
            "diffusion_models": ["flux-2-klein-4b-fp8.safetensors"],
            "text_encoders": ["qwen_3_4b.safetensors"],
            "vae": ["flux2-vae.safetensors"],
        }

    async def get_node_info(_base_url, node_class):
        return flux2_node_info(normalized, node_class)

    async def submit(_base_url, workflow, *, client_id, prompt_id):
        captured["workflow"] = workflow
        captured["prompt_id"] = prompt_id
        return {"promptId": prompt_id, "queueNumber": 1, "nodeErrors": {}}

    async def get_job(_base_url, prompt_id):
        return {
            "promptId": prompt_id,
            "status": "completed",
            "terminal": True,
            "outputs": [{"nodeId": "13", "index": 0, "filename": "klein.png", "subfolder": "Aiso", "storageType": "output"}],
            "error": None,
        }

    async def release_models(_base_url):
        return {"requested": True}

    monkeypatch.setattr(comfy_client, "get_jobs_capability", capability)
    monkeypatch.setattr(comfy_client, "get_models_inventory", get_inventory)
    monkeypatch.setattr(comfy_client, "get_node_info", get_node_info)
    monkeypatch.setattr(comfy_client, "submit_prompt", submit)
    monkeypatch.setattr(comfy_client, "get_job", get_job)
    monkeypatch.setattr(comfy_client, "release_models", release_models)
    return captured


def run(coro):
    return asyncio.run(coro)


def test_tool_schema_exposes_only_user_facing_generation_inputs():
    function = generation.GENERATE_IMAGE_SCHEMA["function"]
    assert function["name"] == "generate_image"
    properties = function["parameters"]["properties"]
    assert {"prompt", "negative_prompt", "model_hint", "width", "height", "seed"} <= set(properties)
    assert {"steps", "cfg", "sampler", "scheduler", "selection_context"}.isdisjoint(properties)


def test_pipeline_snapshot_uses_only_the_selected_image_output_path():
    workflow = {
        "1": {"class_type": "ImageSource", "inputs": {}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        "80": {"class_type": "LatentUpscaleBy", "inputs": {"samples": ["1", 0], "scale_by": 0.5}},
        "81": {"class_type": "SaveImage", "inputs": {"images": ["80", 0]}},
        "90": {"class_type": "VAEDecode", "inputs": {"samples": ["1", 0], "vae": ["1", 1]}},
        "91": {"class_type": "ImageUpscaleWithModel", "inputs": {"image": ["90", 0]}},
        "92": {"class_type": "SaveImage", "inputs": {"images": ["91", 0]}},
    }
    policy = {"id": "flux-negative-unconnected-v1", "addedPositive": []}

    direct = generation._build_pipeline_snapshot(
        workflow,
        output_node_id="2",
        source="user-workflow",
        prompt_policy=policy,
        uses_negative_prompt=False,
        negative_binding_node_ids=("90",),
        effective_negative_prompt="",
    )
    assert direct == {
        "source": "user-workflow",
        "nodeCount": 2,
        "vaeDecode": False,
        "negativeMode": "not-connected",
        "scaleProcess": False,
        "processingNodes": [],
    }

    connected_empty = generation._build_pipeline_snapshot(
        workflow,
        output_node_id="2",
        source="aiso-built-in",
        prompt_policy=policy,
        uses_negative_prompt=True,
        negative_binding_node_ids=(),
        effective_negative_prompt="",
    )
    assert connected_empty is not None
    assert connected_empty["negativeMode"] == "connected-empty"

    downscaled = generation._build_pipeline_snapshot(
        workflow,
        output_node_id="81",
        source="user-workflow",
        prompt_policy=policy,
        uses_negative_prompt=False,
        negative_binding_node_ids=(),
        effective_negative_prompt="",
    )
    assert downscaled == {
        "source": "user-workflow",
        "nodeCount": 3,
        "vaeDecode": False,
        "negativeMode": "not-connected",
        # The badge says only that a scale-processing node is on the path;
        # scale_by=0.5 must never be described as actual enlargement.
        "scaleProcess": True,
        "processingNodes": ["LatentUpscaleBy"],
    }

    upscaled = generation._build_pipeline_snapshot(
        workflow,
        output_node_id="92",
        source="user-workflow",
        prompt_policy=policy,
        uses_negative_prompt=False,
        negative_binding_node_ids=(),
        effective_negative_prompt="",
    )
    assert upscaled == {
        "source": "user-workflow",
        "nodeCount": 4,
        "vaeDecode": True,
        "negativeMode": "not-connected",
        "scaleProcess": True,
        "processingNodes": ["ImageUpscaleWithModel"],
    }


def test_generate_image_uses_typescript_profile_defaults_and_returns_reference_only(monkeypatch):
    profile = raw_profile()
    captured = install_success_fakes(monkeypatch, [profile], seed_output=18_446_744_073_709_551_615)
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt="masterpiece, anime character",
            negative_prompt="bad anatomy",
        )
    )
    image = result["image"]
    assert captured["order"][:3] == ["capability", "inventory", "submit"]
    assert "release" not in captured["order"]
    assert image["profileId"] == "anime_model"
    assert image["modelName"] == "user-anime.safetensors"
    assert (image["width"], image["height"], image["steps"], image["cfg"]) == (832, 1216, 30, 6.0)
    assert image["seed"] == "18446744073709551615"
    assert captured["workflow"]["5"]["inputs"]["seed"] == 18_446_744_073_709_551_615
    assert image["filename"] == "result_00001_.png"
    assert "base64" not in repr(result).casefold()
    assert "bytes" not in image
    assert image["jobId"] == captured["prompt_id"]
    assert image["baseUrl"] == "http://127.0.0.1:8188"
    assert image["originalPrompt"] == "masterpiece, anime character"
    assert image["originalNegativePrompt"] == "bad anatomy"
    assert image["effectivePrompt"] == "masterpiece, anime character"
    assert image["effectiveNegativePrompt"].startswith("bad anatomy, low quality")
    assert image["promptPolicy"]["id"] == "sd-negative-quality-v1"
    assert image["pipeline"] == {
        "source": "aiso-built-in",
        "nodeCount": 7,
        "vaeDecode": True,
        "negativeMode": "conditioning",
        "scaleProcess": False,
        "processingNodes": [],
    }
    assert image["workflow"] == captured["workflow"]
    assert set(image["workflow"]["2"]) == {"class_type", "inputs"}
    assert "작업 ID" in generation.result_to_tool_text(result)


def test_generate_image_uses_optional_builtin_refinement_when_live_contract_is_available(monkeypatch):
    profile = raw_profile(quality_mode="refine")
    captured = install_success_fakes(monkeypatch, [profile])
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt="1girl, pink hair, blue eyes",
        )
    )

    assert captured["workflow"]["8"]["class_type"] == "LatentUpscale"
    assert captured["workflow"]["6"]["inputs"]["samples"] == ["9", 0]
    assert (result["image"]["baseWidth"], result["image"]["baseHeight"]) == (832, 1216)
    assert (result["image"]["width"], result["image"]["height"]) == (1216, 1792)
    assert result["image"]["pipeline"]["nodeCount"] == 9
    assert result["image"]["pipeline"]["scaleProcess"] is True
    assert result["image"]["pipeline"]["processingNodes"] == ["LatentUpscale"]


def test_generate_image_refine_mode_falls_back_when_optional_node_is_not_available(monkeypatch):
    profile = raw_profile(quality_mode="refine")
    captured = install_success_fakes(monkeypatch, [profile])
    original = comfy_client.get_node_info

    async def missing_optional(base_url, node_class):
        if node_class == "LatentUpscale":
            raise comfy_client.ComfyAPIError("required node unavailable")
        return await original(base_url, node_class)

    monkeypatch.setattr(comfy_client, "get_node_info", missing_optional)
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt="1girl, pink hair, blue eyes",
        )
    )

    assert set(captured["workflow"]) == {"1", "2", "3", "4", "5", "6", "7"}
    assert result["image"]["pipeline"]["nodeCount"] == 7
    assert result["image"]["pipeline"]["scaleProcess"] is False


def test_generate_image_manual_profile_id_overrides_prompt_and_model_hint(monkeypatch):
    manual = raw_profile(
        ident="manual_anime",
        name="Manual Anime",
        tag="anime",
        priority=1,
        checkpoint="manual-anime.safetensors",
    )
    automatic = raw_profile(
        ident="automatic_texture",
        name="Automatic Texture",
        tag="texture",
        priority=100,
        checkpoint="automatic-texture.safetensors",
    )
    # This profile is deliberately not an automatic Agent candidate. Manual
    # selection may still use it after all normal workflow/asset checks pass.
    manual["agentEnabled"] = False
    captured = install_success_fakes(
        monkeypatch,
        [manual, automatic],
        allow_manual_only_profiles=True,
    )

    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[manual, automatic],
            prompt="photorealistic texture",
            model_hint="Automatic Texture",
            selected_profile_id="manual_anime",
        )
    )

    assert captured["selected_model"] == "manual-anime.safetensors"
    assert result["image"]["profileId"] == "manual_anime"
    assert "수동" in result["image"]["selectionReason"]


def test_generate_image_compiles_and_submits_flux2_klein_official_contract(monkeypatch):
    profile = raw_flux2_klein_profile()
    captured = install_flux2_success_fakes(monkeypatch, profile)
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt="a silver bob-haired character in a navy futuristic jacket",
            negative_prompt="ignored",
            seed="7",
        )
    )
    workflow = captured["workflow"]
    assert workflow["2"]["inputs"] == {"clip_name": "qwen_3_4b.safetensors", "type": "flux2"}
    assert workflow["9"]["inputs"] == {"steps": 4, "width": 1024, "height": 1024}
    assert workflow["5"]["class_type"] == "ConditioningZeroOut"
    assert result["image"]["profileId"] == "flux2_klein"
    assert result["image"]["originalNegativePrompt"] == "ignored"
    assert result["image"]["effectiveNegativePrompt"] == ""
    assert result["image"]["promptPolicy"]["id"] == "flux-negative-unconnected-v1"
    assert result["image"]["pipeline"] == {
        "source": "aiso-built-in",
        "nodeCount": 13,
        "vaeDecode": True,
        "negativeMode": "not-connected",
        "scaleProcess": False,
        "processingNodes": [],
    }
    assert result["image"]["workflow"] == workflow


def test_generate_image_runs_user_api_workflow_without_family_inventory_assumptions(monkeypatch):
    profile = raw_user_workflow_profile()
    normalized = cw.normalize_profile(profile)
    captured: dict = {"inventory_called": False}

    async def capability(_base_url):
        return {"supported": True}

    async def inventory(*_args, **_kwargs):
        captured["inventory_called"] = True
        raise AssertionError("사용자 워크플로는 family 폴더 inventory를 추측하면 안 됩니다.")

    async def contracted_inventory(*_args, **_kwargs):
        captured["inventory_called"] = True
        return {"checkpoints": ["user-anime.safetensors"]}

    async def node_info(_base_url, node_class):
        base = cw.normalize_profile(raw_profile())
        return sd_node_info(base, node_class)

    async def submit(_base_url, workflow, *, client_id, prompt_id):
        captured["workflow"] = workflow
        return {"promptId": prompt_id, "queueNumber": 1, "nodeErrors": {}}

    async def job(_base_url, prompt_id):
        return {
            "promptId": prompt_id,
            "status": "completed",
            "terminal": True,
            "outputs": [{
                "nodeId": "7", "index": 0, "filename": "user.png",
                "subfolder": "Aiso", "storageType": "output",
            }],
            "error": None,
        }

    async def release(_base_url):
        return {"requested": True}

    monkeypatch.setattr(comfy_client, "get_jobs_capability", capability)
    monkeypatch.setattr(comfy_client, "get_models_inventory", contracted_inventory)
    monkeypatch.setattr(comfy_client, "get_node_info", node_info)
    monkeypatch.setattr(comfy_client, "submit_prompt", submit)
    monkeypatch.setattr(comfy_client, "get_job", job)
    monkeypatch.setattr(comfy_client, "release_models", release)

    result = run(generation.generate_image(
        base_url="http://127.0.0.1:8188",
        profiles=[profile],
        prompt="new architecture prompt",
        negative_prompt="artifact",
        seed="77",
    ))
    assert captured["inventory_called"] is True
    assert captured["workflow"]["2"]["inputs"]["text"] == "new architecture prompt"
    assert captured["workflow"]["3"]["inputs"]["text"] == "artifact"
    assert captured["workflow"]["5"]["inputs"]["seed"] == 77
    assert result["image"]["workflow"] == captured["workflow"]
    assert result["image"]["modelName"] == "user-anime.safetensors"
    assert result["image"]["pipeline"] == {
        "source": "user-workflow",
        "nodeCount": 7,
        "vaeDecode": True,
        "negativeMode": "conditioning",
        "scaleProcess": False,
        "processingNodes": [],
    }


def test_release_failure_does_not_hide_completed_image(monkeypatch, caplog):
    profile = raw_profile()
    install_success_fakes(monkeypatch, [profile])
    monkeypatch.setattr(generation, "RELEASE_MODELS_AFTER_AISO_JOB", True)

    async def fail_release(_base_url):
        raise comfy_client.ComfyAPIError("해제 실패")

    monkeypatch.setattr(comfy_client, "release_models", fail_release)
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt="anime",
        )
    )
    assert result["image"]["filename"] == "result_00001_.png"
    assert "VRAM 해제 요청 실패" in caplog.text


def test_generation_coordinator_only_releases_after_its_last_aiso_job(monkeypatch):
    releases = []

    async def release(base_url):
        releases.append(base_url)
        return {"requested": True}

    monkeypatch.setattr(generation, "RELEASE_MODELS_AFTER_AISO_JOB", True)
    monkeypatch.setattr(comfy_client, "release_models", release)

    async def scenario():
        first = await generation._begin_generation("http://127.0.0.1:8188")
        second = await generation._begin_generation("http://127.0.0.1:8188")
        await generation._finish_generation(
            "http://127.0.0.1:8188", first, submission_attempted=True
        )
        assert releases == []
        await generation._finish_generation(
            "http://127.0.0.1:8188", second, submission_attempted=True
        )

    run(scenario())
    assert releases == ["http://127.0.0.1:8188"]


def test_hidden_selection_context_affects_tags_but_not_workflow_prompt(monkeypatch):
    photo = raw_profile(
        ident="photo", name="Photo", tag="photo", priority=100, checkpoint="photo.safetensors"
    )
    anime = raw_profile(
        ident="anime", name="Anime", tag="anime", priority=1, checkpoint="anime.safetensors"
    )
    captured = install_success_fakes(monkeypatch, [photo, anime])
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[photo, anime],
            prompt="beautiful character",
            selection_context="애니메이션이 아니라 keyword anime 캐릭터를 만들어줘",
        )
    )
    assert result["image"]["profileId"] == "anime"
    assert captured["workflow"]["2"]["inputs"]["text"] == "beautiful character"
    assert "selection_context" not in repr(captured["workflow"])


def test_exact_model_hint_beats_tag_and_priority(monkeypatch):
    high = raw_profile(
        ident="high", name="High", tag="anime", priority=100, checkpoint="high.safetensors"
    )
    exact = raw_profile(
        ident="exact", name="Exact Model", tag="other", priority=-100, checkpoint="exact.safetensors"
    )
    install_success_fakes(monkeypatch, [high, exact])
    result = run(
        generation.generate_image(
            base_url="http://localhost:8188/",
            profiles=[high, exact],
            prompt="anime",
            model_hint="Exact Model",
            seed="9007199254740993",
        )
    )
    assert result["image"]["profileId"] == "exact"
    assert result["image"]["seed"] == "9007199254740993"


def test_missing_jobs_capability_blocks_before_inventory_and_submit(monkeypatch):
    calls = []

    async def unsupported(_base_url):
        calls.append("capability")
        raise comfy_client.ComfyAPIError("단일 작업 API 없음")

    async def should_not_run(*_args, **_kwargs):
        calls.append("unexpected")
        raise AssertionError("must not run")

    monkeypatch.setattr(comfy_client, "get_jobs_capability", unsupported)
    monkeypatch.setattr(comfy_client, "get_models_inventory", should_not_run)
    monkeypatch.setattr(comfy_client, "submit_prompt", should_not_run)
    with pytest.raises(generation.GenerationError, match="단일 작업 API 없음"):
        run(
            generation.generate_image(
                base_url="http://127.0.0.1:8188",
                profiles=[raw_profile()],
                prompt="anime",
            )
        )
    assert calls == ["capability"]


def test_pre_submission_transport_error_is_explicitly_retryable(monkeypatch):
    async def offline(_base_url):
        raise comfy_client.ComfyAPIError("ComfyUI에 연결할 수 없습니다.")

    monkeypatch.setattr(comfy_client, "get_jobs_capability", offline)
    with pytest.raises(generation.GenerationError) as error:
        run(
            generation.generate_image(
                base_url="http://127.0.0.1:8188",
                profiles=[raw_profile()],
                prompt="anime",
            )
        )
    assert error.value.retryable is True
    assert error.value.kind == "transport"


def test_ambiguous_submit_timeout_is_never_retryable_even_when_cancel_fails(monkeypatch):
    profile = raw_profile()
    captured = install_success_fakes(monkeypatch, [profile])
    cancels = []

    async def accepted_then_timeout(_base_url, _workflow, *, client_id, prompt_id):
        captured.setdefault("submissions", []).append((client_id, prompt_id))
        raise comfy_client.ComfyAPIError("ComfyUI 응답 시간이 초과되었습니다.")

    async def cancel_fails(_base_url, prompt_id):
        cancels.append(prompt_id)
        raise comfy_client.ComfyAPIError("ComfyUI에 연결할 수 없습니다.")

    monkeypatch.setattr(comfy_client, "submit_prompt", accepted_then_timeout)
    monkeypatch.setattr(comfy_client, "cancel_job", cancel_fails)
    with pytest.raises(generation.GenerationError) as error:
        run(
            generation.generate_image(
                base_url="http://127.0.0.1:8188",
                profiles=[profile],
                prompt="anime",
            )
        )

    assert error.value.retryable is False
    assert error.value.kind == "terminal"
    assert "작업 취소를 확인하지 못했습니다" in str(error.value)
    assert len(captured["submissions"]) == 1
    assert cancels == [captured["submissions"][0][1]]
    assert "release" not in captured["order"]


@pytest.mark.parametrize(
    ("cancel_result", "expected"),
    [
        ({"promptId": "ignored", "cancelled": True}, "해당 작업을 취소했습니다"),
        ({"promptId": "ignored", "cancelled": False}, "작업 취소를 확인하지 못했습니다"),
        (comfy_client.ComfyAPIError("취소 연결 실패"), "작업 취소를 확인하지 못했습니다"),
    ],
)
def test_generation_deadline_reports_cancel_confirmation_truthfully(monkeypatch, cancel_result, expected):
    async def cancel(_base_url, _prompt_id):
        if isinstance(cancel_result, Exception):
            raise cancel_result
        return cancel_result

    monkeypatch.setattr(generation, "GENERATION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(comfy_client, "cancel_job", cancel)
    with pytest.raises(generation.GenerationError, match=expected) as error:
        run(generation._wait_for_terminal_job("http://127.0.0.1:8188", "job-id"))
    assert error.value.retryable is False


def test_cancelled_agent_request_targets_submitted_prompt_only(monkeypatch):
    profile = raw_profile()
    captured = install_success_fakes(monkeypatch, [profile])
    cancelled = []

    async def interrupted(_base_url, _prompt_id):
        raise asyncio.CancelledError

    async def cancel(base_url, prompt_id):
        cancelled.append((base_url, prompt_id))
        return {"promptId": prompt_id, "cancelled": True}

    monkeypatch.setattr(comfy_client, "get_job", interrupted)
    monkeypatch.setattr(comfy_client, "cancel_job", cancel)
    with pytest.raises(asyncio.CancelledError):
        run(
            generation.generate_image(
                base_url="http://127.0.0.1:8188",
                profiles=[profile],
                prompt="anime",
            )
    )
    assert cancelled == [("http://127.0.0.1:8188", captured["prompt_id"])]
    assert "release" not in captured["order"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_failure_never_claims_an_image(monkeypatch, status):
    profile = raw_profile()
    captured = install_success_fakes(monkeypatch, [profile])

    async def terminal(_base_url, prompt_id):
        return {
            "promptId": prompt_id,
            "status": status,
            "terminal": True,
            "outputs": [],
            "error": {"type": "SeedError", "message": "safe"} if status == "failed" else None,
        }

    monkeypatch.setattr(comfy_client, "get_job", terminal)
    with pytest.raises(generation.GenerationError) as error:
        run(
            generation.generate_image(
                base_url="http://127.0.0.1:8188",
                profiles=[profile],
                prompt="anime",
            )
        )
    assert error.value.kind == "terminal"
    assert "release" not in captured["order"]


def test_local_input_validation_is_structured_as_input_error():
    with pytest.raises(generation.GenerationError) as error:
        run(
            generation.generate_image(
                base_url="http://127.0.0.1:8188",
                profiles=[raw_profile()],
                prompt="",
            )
        )
    assert error.value.kind == "input"


def test_prompt_policy_records_effective_sd_negative_and_safe_workflow(monkeypatch):
    profile = raw_profile(name="User-supplied model", tag="character")
    captured = install_success_fakes(monkeypatch, [profile])
    prompt = "blue hair, 1girl, masterpiece, blue hair, cherry blossoms"
    negative_prompt = "bad hands, custom artifact"
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed="7",
        )
    )
    image = result["image"]
    assert image["originalPrompt"] == prompt
    assert image["originalNegativePrompt"] == negative_prompt
    assert image["effectivePrompt"] == prompt
    assert image["effectiveNegativePrompt"].startswith(negative_prompt)
    assert "low quality" in image["effectiveNegativePrompt"]
    assert image["prompt"] == prompt
    assert captured["workflow"]["2"]["inputs"]["text"] == prompt
    assert captured["workflow"]["3"]["inputs"]["text"] == image["effectiveNegativePrompt"]
    assert image["workflow"] == captured["workflow"]
    assert image["workflow"]["5"]["inputs"]["positive"] == ["2", 0]
    assert "client_id" not in repr(image["workflow"])
    assert "X-Aiso-Token" not in repr(image["workflow"])
    assert set(image["promptPolicy"]) == {
        "id",
        "label",
        "description",
        "addedPositive",
        "addedNegative",
    }
    assert image["promptPolicy"] == {
        "id": "sd-negative-quality-v1",
        "label": "SD 품질 네거티브",
        "description": "SD 계열의 실제 네거티브 조건에 기본 품질 항목을 합치며, 인물 요청일 때만 손·해부학 항목을 추가합니다.",
        "addedPositive": [],
        "addedNegative": [
            "low quality", "low resolution", "blurry", "jpeg artifacts", "bad anatomy",
            "malformed hands", "extra fingers", "missing fingers", "fused fingers",
        ],
    }
    assert "프롬프트 정책: SD 품질 네거티브 (sd-negative-quality-v1)" in generation.result_to_tool_text(result)


def test_animagine_generation_metadata_matches_submitted_workflow(monkeypatch):
    profile = raw_profile(
        ident="animagine-xl-4-opt",
        name="Animagine XL 4.0 Opt",
        checkpoint="animagine-xl-4.0-opt.safetensors",
    )
    profile["assets"][0]["sha256"] = "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
    captured = install_success_fakes(monkeypatch, [profile])

    result = run(generation.generate_image(
        base_url="http://127.0.0.1:8188",
        profiles=[profile],
        prompt="Hatsune Miku; blue hair, no weapon, masterpiece",
        negative_prompt="custom artifact",
        seed="17",
    ))

    image = result["image"]
    assert image["promptPolicy"]["id"] == cw.ANIMAGINE_XL_4_POLICY_ID
    assert image["promptPolicy"]["contract"]["variant"] == "4.0 Opt"
    assert image["effectivePrompt"] == (
        "1girl, Hatsune Miku, blue hair, masterpiece, high score, great score, absurdres"
    )
    assert image["effectiveNegativePrompt"].startswith("custom artifact, weapon, lowres")
    assert captured["workflow"]["2"]["inputs"]["text"] == image["effectivePrompt"]
    assert captured["workflow"]["3"]["inputs"]["text"] == image["effectiveNegativePrompt"]
    assert image["promptNormalization"] == {
        "positiveChanged": True,
        "negativeChanged": True,
        "positiveLength": len(image["effectivePrompt"]),
        "negativeLength": len(image["effectiveNegativePrompt"]),
        "maxPromptLength": cw.MAX_PROMPT_LENGTH,
    }
    assert cw.ANIMAGINE_XL_4_POLICY_LABEL in result["summary"]
    assert (
        f"프롬프트 정책: {cw.ANIMAGINE_XL_4_POLICY_LABEL} ({cw.ANIMAGINE_XL_4_POLICY_ID})"
        in generation.result_to_tool_text(result)
    )
    assert "6327eca98b" not in repr(image["workflow"])
    assert "X-Aiso-Token" not in repr(image["workflow"])


# ── A8: 사용자 워크플로의 크기는 추측하지 않는다 ──────────────────────────

def test_user_workflow_dimensions_are_not_guessed_from_a_stray_upscale_node():
    """사용자 워크플로에는 선언된 옵션 크기를 그대로 보고한다.

    `_delivered_dimensions`는 LatentUpscale 한 클래스만 이해하고, 찾은 첫 노드가
    출력 경로 위인지도 확인하지 않는다. 그래서 사용자 워크플로를 훑으면 실제 출력이
    2048x2048인 그래프에서 경로 밖 512x512 노드를 집어 보고할 수 있었다.
    그 숫자는 UI 화살표 표기와 에이전트 답변 근거로 그대로 나간다.

    넓혀서 고치지 않는 이유: 사용자 그래프는 LatentUpscaleBy·ImageScale·
    ImageUpscaleWithModel을 흔히 쓴다. 부분적으로만 맞는 숫자는 틀린 숫자다.
    사용자 워크플로의 정직한 신호는 숫자가 아니라 pipeline 배지다.
    """
    from comfy_generation import _delivered_dimensions

    workflow = {
        "9": {"class_type": "LatentUpscale", "inputs": {"width": 512, "height": 512}},
        "10": {"class_type": "LatentUpscale", "inputs": {"width": 2048, "height": 2048}},
    }

    assert _delivered_dimensions(
        workflow, fallback_width=1024, fallback_height=1024, builtin=False
    ) == (1024, 1024)


def test_builtin_workflow_still_reports_its_refined_latent_size():
    """Aiso가 직접 조립한 그래프에서는 기존 동작 그대로 — 보정 후 크기를 보고한다."""
    from comfy_generation import _delivered_dimensions

    workflow = {"9": {"class_type": "LatentUpscale", "inputs": {"width": 2048, "height": 2048}}}

    assert _delivered_dimensions(
        workflow, fallback_width=1024, fallback_height=1024, builtin=True
    ) == (2048, 2048)
