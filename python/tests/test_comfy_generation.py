# -*- coding: utf-8 -*-
"""모델 자동 선택부터 ComfyUI jobs poll/output ref까지의 생성 서비스 테스트."""

from __future__ import annotations

import asyncio
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
) -> dict:
    return {
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


def install_success_fakes(monkeypatch, profiles: list[dict], *, seed_output: int = 42) -> dict:
    normalized = [cw.normalize_profile(profile) for profile in profiles]
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


def run(coro):
    return asyncio.run(coro)


def test_tool_schema_exposes_only_user_facing_generation_inputs():
    function = generation.GENERATE_IMAGE_SCHEMA["function"]
    assert function["name"] == "generate_image"
    properties = function["parameters"]["properties"]
    assert {"prompt", "negative_prompt", "model_hint", "width", "height", "seed"} <= set(properties)
    assert {"steps", "cfg", "sampler", "scheduler", "selection_context"}.isdisjoint(properties)


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
    assert captured["order"][-1] == "release"
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
    assert image["effectivePrompt"] == "masterpiece, anime character"
    assert image["effectiveNegativePrompt"] == "bad anatomy"
    assert image["promptPolicy"]["id"] == "none"
    assert image["workflow"] == captured["workflow"]
    assert set(image["workflow"]["2"]) == {"class_type", "inputs"}
    assert "작업 ID" in generation.result_to_tool_text(result)


def test_release_failure_does_not_hide_completed_image(monkeypatch, caplog):
    profile = raw_profile()
    install_success_fakes(monkeypatch, [profile])

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
    assert captured["order"][-1] == "release"


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
    assert captured["order"][-1] == "release"


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
    assert captured["order"][-1] == "release"


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


def test_animagine_policy_effective_prompts_and_actual_safe_workflow_are_returned(monkeypatch):
    profile = raw_profile(
        name="My verified Animagine",
        tag="animagine-xl-4.0-opt",
        checkpoint="renamed-animagine.safetensors",
    )
    profile["assets"][0]["sha256"] = (
        "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac"
    )
    captured = install_success_fakes(monkeypatch, [profile])
    result = run(
        generation.generate_image(
            base_url="http://127.0.0.1:8188",
            profiles=[profile],
            prompt="blue hair, 1girl, masterpiece, blue hair, cherry blossoms",
            negative_prompt="bad hands, custom artifact",
            seed="7",
        )
    )
    image = result["image"]
    expected_positive = (
        "1girl, blue hair, cherry blossoms, masterpiece, high score, great score, absurdres"
    )
    assert image["originalPrompt"] == (
        "blue hair, 1girl, masterpiece, blue hair, cherry blossoms"
    )
    assert image["effectivePrompt"] == expected_positive
    assert image["effectiveNegativePrompt"].startswith("custom artifact, lowres, bad anatomy")
    assert image["prompt"] == expected_positive
    assert captured["workflow"]["2"]["inputs"]["text"] == expected_positive
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
    assert image["promptPolicy"]["id"] == cw.ANIMAGINE_XL_4_POLICY_ID
    assert image["promptPolicy"]["addedPositive"] == [
        "high score",
        "great score",
        "absurdres",
    ]
    assert "SHA-256" in image["promptPolicy"]["description"]
    assert cw.ANIMAGINE_XL_4_POLICY_LABEL in result["summary"]
    assert cw.ANIMAGINE_XL_4_POLICY_LABEL in generation.result_to_tool_text(result)
