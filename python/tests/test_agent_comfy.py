# -*- coding: utf-8 -*-
"""Agent와 ComfyUI 생성 도구 사이의 조건부 노출·이벤트 계약."""

from __future__ import annotations

import copy

import agent
import pytest
from conftest import FakeChat, types


PROFILE = {
    "id": "anime-sdxl",
    "name": "Anime SDXL",
    "family": "sdxl",
    "capabilities": ["txt2img"],
    "tags": ["anime", "character"],
    "agentEnabled": True,
    "priority": 10,
    "assets": [
        {
            "id": "checkpoint-1",
            "kind": "checkpoint",
            "slot": "checkpoint",
            "fileName": "anime.safetensors",
            "comfyName": "anime.safetensors",
            "relativePath": "checkpoints/anime.safetensors",
            "size": 123,
            "sha256": "a" * 64,
        }
    ],
    "workflowTemplateId": "sdxl.txt2img.v1",
    "defaults": {
        "width": 1024,
        "height": 1024,
        "steps": 28,
        "cfg": 5,
        "sampler": "euler_ancestral",
        "scheduler": "normal",
    },
}

IMAGE_REQUEST = "애니메이션 캐릭터 이미지를 생성해줘."
IMAGE_MESSAGES = [{"role": "user", "content": IMAGE_REQUEST}]


def workflow_snapshot() -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "anime.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "1girl", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "seed": 42, "steps": 28, "cfg": 5,
                "sampler_name": "euler_ancestral", "scheduler": "normal",
                "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0], "denoise": 1,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "Aiso/test"}},
    }


def generated_result() -> dict:
    return {
        "summary": "이미지 생성 완료",
        "image": {
            "jobId": "11111111-1111-4111-8111-111111111111",
            "filename": "Aiso_00001_.png",
            "subfolder": "Aiso/agent",
            "storageType": "output",
            "baseUrl": "http://127.0.0.1:8188",
            "profileId": "anime-sdxl",
            "profileName": "Anime SDXL",
            "modelName": "anime.safetensors",
            "selectionReason": "anime 태그 일치",
            "prompt": "1girl, original character",
            "effectivePrompt": "1girl, original character, masterpiece",
            "negativePrompt": "low quality",
            "workflow": workflow_snapshot(),
            "seed": "42",
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "cfg": 5,
            "sampler": "euler_ancestral",
            "scheduler": "normal",
        },
    }


def test_image_request_detection_accepts_draw_followup_without_generic_create_false_positive():
    assert agent._looks_like_image_generation_request("프리렌 그려줘")
    assert agent._looks_like_image_generation_request("1번 스타일로 그려줘")
    assert not agent._looks_like_image_generation_request(
        "그걸로 진행해줘", "ComfyUI 이미지 Prompt: 1girl, elf"
    )
    assert not agent._looks_like_image_generation_request(
        "그걸로 진행해줘", "ComfyUI 이미지 API를 연결하면 됩니다."
    )
    assert not agent._looks_like_image_generation_request("마크다운 문서를 만들어줘")
    assert not agent._looks_like_image_generation_request("이미지 생성하지 마세요")
    assert not agent._looks_like_image_generation_request("그림 생성 기능을 어떻게 만들어요?")
    assert not agent._looks_like_image_generation_request("그림 생성 기능을 만들어주세요")
    assert not agent._looks_like_image_generation_request("ComfyUI 이미지 생성 버튼을 만들어줘")
    assert not agent._looks_like_image_generation_request("이미지 생성 모듈을 만들어줘")
    assert not agent._looks_like_image_generation_request("캐릭터 이미지를 생성하는 파이썬 코드를 만들어줘")
    assert not agent._looks_like_image_generation_request("Create an image generation feature for Aiso")
    assert not agent._looks_like_image_generation_request("Create a Python module that generates an image")
    assert not agent._looks_like_image_generation_request("프로젝트 구조를 다이어그램으로 그려줘")
    assert agent._looks_like_image_generation_request("이미지를 생성하고 결과를 기록해줘")
    assert agent._looks_like_image_generation_request("프리렌을 그려서 파일로 저장해줘")
    assert agent._looks_like_image_generation_request("프리렌 이미지 생성 좀 해줘")
    assert agent._looks_like_image_generation_request("프리렌 이미지 생성 부탁해")
    assert not agent._looks_like_image_generation_request("이미지를 생성하고 저장하는 법을 설명해줘")
    assert not agent._looks_like_image_generation_request("그림을 그려서 저장하는 방법을 알려줘")
    assert not agent._looks_like_image_generation_request("ComfyUI에서 이미지를 생성하고 저장하려면?")
    assert not agent._looks_like_image_generation_request("그림을 그려서 저장하는 과정을 보여줘")
    assert agent._looks_like_image_generation_request("프리렌을 그려줘. 끝나면 알려줘")
    assert agent._looks_like_image_generation_request("프리렌이 어떻게 웃는지 그려줘")
    assert agent._looks_like_image_generation_request(
        "‘포기하지 마’라는 문구가 들어간 포스터 이미지를 생성해줘"
    )
    assert agent._looks_like_image_generation_request(
        "포기하지 마 문구가 들어간 포스터 이미지를 생성해줘"
    )
    assert agent._looks_like_image_generation_request(
        "김치 만드는 과정을 한 장의 일러스트로 그려줘"
    )
    assert agent._looks_like_image_generation_request("프리렌 이미지 생성 한 번 해줘")
    assert agent._looks_like_image_generation_request("프리렌 이미지 생성 하나만 해줘")
    assert agent._looks_like_image_generation_request("프리렌 그림 생성 한 장 부탁해")
    assert agent._looks_like_image_generation_request("프리렌 이미지 생성 두 장 해줘")
    assert agent._looks_like_image_generation_request("프리렌 이미지를 만들어 줄래?")
    assert agent._looks_like_image_generation_request("프리렌 일러스트를 한 장 부탁해")
    assert agent._looks_like_image_generation_request("프리렌 캐릭터 하나 부탁해")
    assert agent._looks_like_image_generation_request(
        "Create an image of a futuristic game UI with red buttons"
    )
    assert not agent._looks_like_image_generation_request(
        "이미지를 생성하고 저장할 수 있는지 설명해줘"
    )
    assert not agent._looks_like_image_generation_request(
        "프리렌을 그려서 저장할 수 있는지 설명해줘"
    )
    assert not agent._looks_like_image_generation_request("이미지를 생성하고 싶지 않아")
    assert not agent._looks_like_image_generation_request("그림을 그려서 보낼 필요는 없어")
    assert not agent._looks_like_image_generation_request("그림을 그려서 보내고 싶지 않아")
    assert not agent._looks_like_image_generation_request("I don't want you to generate an image")
    assert not agent._looks_like_image_generation_request(
        "‘이미지를 생성해줘’라는 문장을 영어로 번역해줘"
    )
    assert not agent._looks_like_image_generation_request(
        '문서에서 "그림을 그려줘"라는 지시를 찾아줘'
    )
    assert not agent._looks_like_image_generation_request(
        "그림을 그려서 저장하는 방법을 요약해줘"
    )
    assert not agent._looks_like_image_generation_request(
        "이미지를 생성하고 저장하는 과정을 문서로 정리해줘"
    )
    assert agent._looks_like_image_generation_request(
        "같은 seed로 눈 색만 파랗게 바꿔줘",
        "이미지 생성을 완료했습니다. 결과 카드에서 확인하세요. 실제 프롬프트: 1girl, green eyes",
    )
    completed = "이미지 생성을 완료했습니다. 결과 카드에서 확인하세요. 실제 프롬프트: 1girl"
    assert not agent._looks_like_image_generation_request("같은 모델 이름이 뭐야?", completed)
    assert not agent._looks_like_image_generation_request("같은 seed는 어디서 확인해?", completed)
    assert not agent._looks_like_image_generation_request("same model means what?", completed)
    assert not agent._looks_like_image_generation_request("change the model setting where?", completed)


def test_completion_history_escapes_prompt_markdown_and_external_url():
    image = generated_result()["image"]
    image["profileName"] = "![profile](HTTPS://invented.invalid/profile.png)"
    image["effectivePrompt"] = (
        "![fake](HTTPS://invented.invalid/prompt.png), WWW.evil.test, <img src=x>, "
        "https&colon;//entity.invalid, foo@example.com"
    )
    text = agent._image_completion_text([image])
    assert "![" not in text
    assert "://" not in text
    assert "WWW." not in text
    assert "<img" not in text
    assert "&colon;" not in text
    assert "@" not in text
    assert "seed 42" in text


@pytest.mark.parametrize(
    "model_text",
    [
        "[결과](https&colon;//invented.invalid/result.png)",
        "www.invented.invalid/result.png",
        "[결과](//invented.invalid/result.png)",
        "[결과](relative.png)",
        "[결과](/relative/result.png)",
        "[결과](irc://invented.invalid/result.png)",
        "foo@example.com",
    ],
)
def test_image_turn_text_blocks_entity_protocol_relative_and_autolinks(model_text):
    assert "도구가 완료되지 않아" in agent._safe_image_turn_text(model_text)


def test_completion_history_keeps_each_multi_image_prompt():
    first = generated_result()["image"]
    second = copy.deepcopy(first)
    first["effectivePrompt"] = "first variant"
    second["effectivePrompt"] = "second variant"
    second["seed"] = "43"
    text = agent._image_completion_text([first, second])
    assert "결과 1 실제 프롬프트: first variant" in text
    assert "결과 2 실제 프롬프트: second variant" in text
    assert "결과 2: 모델 Anime SDXL, seed 43" in text


def test_generate_image_is_conditionally_exposed_and_emits_reference(env, monkeypatch):
    seen: dict = {}

    async def fake_unload(host):
        seen["unload_host"] = host
        return ["local-llm"]

    async def fake_generate_image(**kwargs):
        seen["generate"] = kwargs
        return generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat(
        [
            {
                "content": "![조작 이미지](https://invented.invalid/fake.png)",
                "calls": [
                    (
                        "generate_image",
                        {
                            "prompt": "1girl, original character",
                            "negative_prompt": "low quality",
                            "seed": 42,
                            # 로컬 LLM이 스키마 밖의 기술값을 추측해도 Agent 경계에서 제거한다.
                            "steps": 30,
                            "cfg": 7,
                            "sampler": "Euler a",
                            "scheduler": "UniPC",
                        },
                    )
                ]
            },
            {"content": "생성된 이미지를 확인해 주세요."},
        ]
    )

    events = env.drive(
        chat,
        approve=True,
        messages=IMAGE_MESSAGES,
        workspace="",
        approval_mode="read",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )

    exposed = {tool["function"]["name"] for tool in chat.payloads[0]["tools"]}
    assert "generate_image" in exposed
    assert seen["unload_host"] == "h"
    assert seen["generate"]["profiles"] == [PROFILE]
    assert seen["generate"]["base_url"] == "http://127.0.0.1:8188"
    assert seen["generate"]["selection_context"] == IMAGE_REQUEST
    assert "steps" not in seen["generate"]
    assert "cfg" not in seen["generate"]
    assert "sampler" not in seen["generate"]
    assert "scheduler" not in seen["generate"]
    assert "approval_request" in types(events)
    assert types(events).index("approval_request") < types(events).index("tool_result")
    assert types(events).index("image_result") == types(events).index("tool_result") + 1
    image_event = next(event for event in events if event["type"] == "image_result")
    assert image_event["image"]["filename"] == "Aiso_00001_.png"
    assert len(image_event["image"]["workflow"]) == 7
    assert chat.calls == 1  # 결과 카드를 낸 뒤 URL을 지어낼 수 있는 불필요한 후속 LLM 턴은 열지 않는다.
    final_text = "".join(event.get("text", "") for event in events if event.get("type") == "content")
    assert "실제 ComfyUI 노드 워크플로" in final_text
    assert "모델 Anime SDXL, seed 42, 크기 1024x1024" in final_text
    assert "실제 프롬프트: 1girl, original character, masterpiece" in final_text
    assert "http" not in final_text
    assert "![" not in final_text
    assert types(events)[-1] == "done"


def test_manual_comfy_selection_forces_the_exact_profile_id(env, monkeypatch):
    seen: dict = {}

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        seen.update(kwargs)
        return generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat([
        {"calls": [("generate_image", {"prompt": "1girl", "model_hint": "another model"})]}
    ])

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        comfy_selection_mode="manual",
        selected_comfy_model_id="anime-sdxl",
        approval_mode="auto",
    )

    assert seen["selected_profile_id"] == "anime-sdxl"
    assert seen["model_hint"] == "another model"
    system_prompt = chat.payloads[0]["messages"][0]["content"]
    assert "직접 선택한 등록 모델은 이미 고정" in system_prompt
    assert "image_result" in types(events)


def test_manual_comfy_selection_without_a_registered_choice_never_falls_back(env, monkeypatch):
    attempted = False

    async def should_not_generate(**_kwargs):
        nonlocal attempted
        attempted = True
        return generated_result()

    monkeypatch.setattr(agent, "generate_image", should_not_generate)
    chat = FakeChat([{"calls": [("generate_image", {"prompt": "1girl"})]}])

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
        comfy_selection_mode="manual",
    )

    assert attempted is False
    assert chat.calls == 0
    assert types(events) == ["error", "done"]
    assert "등록 모델 하나를 선택" in events[0]["error"]


def test_completed_meta_plan_plus_successful_image_finishes_without_followup_llm_url(env, monkeypatch):
    async def fake_unload(_host):
        return []

    async def fake_generate_image(**_kwargs):
        return generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat(
        [
            {
                "calls": [
                    ("update_plan", {"steps": [{"content": "이미지 생성", "status": "completed"}]}),
                    ("generate_image", {"prompt": "1girl"}),
                ]
            },
            {"content": "![조작 이미지](https://invented.invalid/after.png)"},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert chat.calls == 1
    assert "image_result" in types(events)
    content = "".join(event.get("text", "") for event in events if event.get("type") == "content")
    assert "실제 ComfyUI 노드 워크플로" in content
    assert "http" not in content
    assert "![" not in content


def test_plan_completion_after_image_finishes_without_third_llm_turn(env, monkeypatch):
    async def fake_unload(_host):
        return []

    async def fake_generate_image(**_kwargs):
        return generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat(
        [
            {
                "calls": [
                    ("update_plan", {"steps": [{"content": "이미지 생성", "status": "in_progress"}]}),
                    ("generate_image", {"prompt": "1girl"}),
                ]
            },
            {"calls": [("update_plan", {"steps": [{"content": "이미지 생성", "status": "completed"}]})]},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert chat.calls == 2
    assert "image_result" in types(events)
    final_content = "".join(
        event.get("text", "") for event in events if event.get("type") == "content"
    )
    assert "모델 Anime SDXL, seed 42, 크기 1024x1024" in final_content
    assert "실제 프롬프트: 1girl, original character, masterpiece" in final_content


def test_multi_image_terminal_failure_preserves_verified_success_context(env, monkeypatch):
    attempts = 0

    async def fake_unload(_host):
        return []

    async def first_succeeds_second_fails(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return generated_result()
        raise agent.GenerationError("ComfyUI 이미지 생성에 실패했습니다 (RuntimeError).")

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", first_succeeds_second_fails)
    chat = FakeChat(
        [
            {
                "calls": [
                    ("generate_image", {"prompt": "first variant"}),
                    ("generate_image", {"prompt": "second variant"}),
                ]
            }
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert attempts == 2
    assert chat.calls == 1
    assert sum(event.get("type") == "image_result" for event in events) == 1
    final_content = "".join(
        event.get("text", "") for event in events if event.get("type") == "content"
    )
    assert "모델 Anime SDXL, seed 42, 크기 1024x1024" in final_content
    assert "실제 프롬프트: 1girl, original character, masterpiece" in final_content
    summary_index = next(
        index
        for index, event in enumerate(events)
        if event.get("type") == "content" and "모델 Anime SDXL, seed 42" in event.get("text", "")
    )
    error_index = next(index for index, event in enumerate(events) if event.get("type") == "error")
    assert summary_index < error_index
    assert types(events)[-1] == "done"


def test_same_environment_generation_error_retries_once_then_stops_without_web_search(env, monkeypatch):
    attempts: list[dict] = []

    async def fake_unload(_host):
        return []

    async def fail_generation(**kwargs):
        attempts.append(kwargs)
        raise agent.GenerationError(
            "ComfyUI에 연결할 수 없습니다.", retryable=True, kind="transport"
        )

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fail_generation)
    chat = FakeChat(
        [
            {"calls": [("generate_image", {"prompt": "1girl"})]},
            {"calls": [("web_search", {"query": "ComfyUI API 오류"})]},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert chat.calls == 1  # 두 번째 LLM 턴으로 넘어가지 않아 scripted web_search는 실행되지 않는다.
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "generate_image"
    ]
    assert sum(event.get("type") == "notice" and "한 번만 자동 재시도" in event.get("text", "") for event in events) == 1
    assert sum(event.get("type") == "tool_result" and event.get("ok") is False for event in events) == 1
    final_error = next(event["error"] for event in events if event.get("type") == "error")
    assert "최초 시도와 1회 자동 재시도" in final_error
    assert "웹 검색" in final_error
    assert types(events)[-1] == "done"


def test_environment_generation_error_can_recover_on_single_internal_retry(env, monkeypatch):
    attempts = 0

    async def fake_unload(_host):
        return []

    async def flaky_generation(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise agent.GenerationError(
                "ComfyUI가 HTTP 503 오류를 반환했습니다.", retryable=True, kind="transport"
            )
        return generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", flaky_generation)
    chat = FakeChat(
        [
            {"calls": [("generate_image", {"prompt": "1girl"})]},
            {"content": "재시도에서 이미지를 생성했습니다."},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert attempts == 2
    assert "image_result" in types(events)
    assert not any(event.get("type") == "error" for event in events)


@pytest.mark.parametrize(
    "detail",
    [
        "ComfyUI 이미지 생성 작업이 취소되었습니다.",
        "ComfyUI 이미지 생성 제한 시간 30분을 초과해 해당 작업을 취소했습니다.",
        "ComfyUI 이미지 생성에 실패했습니다 (SeedError).",
    ],
)
def test_cancel_and_generation_timeout_are_terminal_without_auto_retry(env, monkeypatch, detail):
    attempts = 0

    async def fake_unload(_host):
        return []

    async def terminal_generation(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise agent.GenerationError(detail)

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", terminal_generation)
    chat = FakeChat(
        [
            {"calls": [("generate_image", {"prompt": "1girl"})]},
            {"calls": [("web_search", {"query": "ComfyUI 재시도"})]},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert attempts == 1
    assert chat.calls == 1
    assert not any("자동 재시도합니다" in event.get("text", "") for event in events)
    final_error = next(event["error"] for event in events if event.get("type") == "error")
    assert detail in final_error
    assert "자동 재시도하지 않았습니다" in final_error
    assert types(events)[-1] == "done"


def test_execution_seed_error_cannot_retry_when_retryable_flag_is_incorrect(env, monkeypatch):
    """제출 후 실행 오류는 문자열이나 잘못된 retryable 플래그와 무관하게 terminal이다."""
    attempts = 0

    async def fake_unload(_host):
        return []

    async def execution_failure(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise agent.GenerationError(
            "ComfyUI 이미지 생성에 실패했습니다 (SeedError).",
            retryable=True,
            kind="terminal",
        )

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", execution_failure)
    events = env.run(
        FakeChat(
            [
                {"calls": [("generate_image", {"prompt": "1girl"})]},
                {"calls": [("web_search", {"query": "ComfyUI 재시도"})]},
            ]
        ),
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert attempts == 1
    assert not any("자동 재시도합니다" in event.get("text", "") for event in events)
    assert "SeedError" in next(event["error"] for event in events if event.get("type") == "error")


def test_generation_input_error_keeps_existing_llm_correction_flow_without_auto_retry(env, monkeypatch):
    attempts = 0

    async def fake_unload(_host):
        return []

    async def invalid_generation(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise agent.GenerationError("프롬프트는 1~4,000자여야 합니다.", kind="input")

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", invalid_generation)
    chat = FakeChat(
        [
            {"calls": [("generate_image", {"prompt": "x"})]},
            {"content": "프롬프트 입력을 수정해 다시 요청해 주세요."},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert attempts == 1
    assert chat.calls == 2
    assert not any(
        event.get("type") == "notice" and "자동 재시도" in event.get("text", "")
        for event in events
    )
    assert types(events)[-1] == "done"


def test_corrected_image_input_error_finishes_after_success_without_third_llm_turn(env, monkeypatch):
    attempts = 0

    async def fake_unload(_host):
        return []

    async def invalid_then_valid(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise agent.GenerationError("프롬프트는 1~4,000자여야 합니다.", kind="input")
        return generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", invalid_then_valid)
    chat = FakeChat(
        [
            {"calls": [("generate_image", {"prompt": "x"})]},
            {"calls": [("generate_image", {"prompt": "1girl"})]},
            {"content": "![조작 링크](https://invented.invalid/third-turn.png)"},
        ]
    )

    events = env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    assert attempts == 2
    assert chat.calls == 2
    assert sum(event.get("type") == "image_result" for event in events) == 1
    final_content = "".join(
        event.get("text", "") for event in events if event.get("type") == "content"
    )
    assert "실제 프롬프트: 1girl, original character, masterpiece" in final_content
    assert "invented.invalid" not in final_content
    assert types(events)[-1] == "done"


def test_image_tool_schema_keeps_model_owned_generation_values_hidden():
    properties = agent.GENERATE_IMAGE_SCHEMA["function"]["parameters"]["properties"]
    assert {"prompt", "negative_prompt", "model_hint", "width", "height", "seed"} <= set(properties)
    assert {"steps", "cfg", "sampler", "scheduler"}.isdisjoint(properties)


def test_long_image_selection_context_keeps_head_and_tail_within_backend_limit():
    request = "START-MODEL " + ("x" * 5_000) + " END-STYLE\x00"
    bounded = agent._bounded_image_selection_context(request)
    assert len(bounded) == agent.MAX_PROMPT_LENGTH
    assert bounded.startswith("START-MODEL")
    assert bounded.endswith("END-STYLE")
    assert "\x00" not in bounded


def test_agent_registered_model_data_has_no_model_specific_prompt_policy_hint(env):
    chat = FakeChat([{"content": "확인했습니다."}])
    env.run(
        chat,
        messages=IMAGE_MESSAGES,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )
    system_prompt = chat.payloads[0]["messages"][0]["content"]
    assert '"promptPolicy"' not in system_prompt
    assert "전용 문법" not in system_prompt
    assert "자동으로 추가하지 않습니다" not in system_prompt


def test_clear_image_request_is_nudged_once_when_model_asks_again(env, monkeypatch):
    async def fake_unload(_host):
        return []

    async def fake_generate_image(**_kwargs):
        return {
            "summary": "완료",
            "image": {
                "jobId": "11111111-1111-4111-8111-111111111111",
                "filename": "result.png",
                "subfolder": "Aiso",
                "storageType": "output",
                "baseUrl": "http://127.0.0.1:8188",
                "profileId": "anime-sdxl",
                "profileName": "Anime SDXL",
                "modelName": "anime.safetensors",
                "selectionReason": "태그 일치",
                "prompt": "silver-haired anime character",
                "negativePrompt": "",
                "seed": "123456",
                "width": 1024,
                "height": 1024,
                "steps": 28,
                "cfg": 5,
                "sampler": "euler_ancestral",
                "scheduler": "normal",
            },
        }

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat(
        [
            {"content": "원하는 구도를 더 알려주세요."},
            {"calls": [("generate_image", {"prompt": "silver-haired anime character", "seed": "123456"})]},
            {"content": "생성했습니다."},
        ]
    )
    events = env.run(
        chat,
        messages=[
            {
                "role": "user",
                "content": "은색 단발 여성 애니메이션 캐릭터 이미지를 1024x1024로 생성해줘.",
            }
        ],
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )
    assert "notice" in types(events)
    assert "image_result" in types(events)
    assert sum(event["type"] == "image_result" for event in events) == 1


def test_generate_image_is_hidden_and_blocked_without_explicit_user_intent(env, monkeypatch):
    attempts = 0

    async def should_not_generate(**_kwargs):
        nonlocal attempts
        attempts += 1
        return generated_result()

    monkeypatch.setattr(agent, "generate_image", should_not_generate)
    chat = FakeChat(
        [
            {"calls": [("generate_image", {"prompt": "unrequested image"})]},
            {"content": "요청하신 문서 설명만 계속하겠습니다."},
        ]
    )
    events = env.run(
        chat,
        messages=[{"role": "user", "content": "마크다운 문서 구조를 설명해줘."}],
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in chat.payloads[0]["tools"]}
    assert "generate_image" not in exposed
    assert attempts == 0
    assert "image_result" not in types(events)
    blocked = next(event for event in events if event.get("type") == "tool_result")
    assert blocked["ok"] is False
    assert "명확한 이미지 생성 지시가 없어" in blocked["output"]


def test_generate_image_is_not_exposed_without_registered_profile(env):
    chat = FakeChat([{"content": "등록 모델이 없습니다."}])
    events = env.run(
        chat,
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[],
    )
    exposed = {tool["function"]["name"] for tool in chat.payloads[0]["tools"]}
    assert "generate_image" not in exposed
    assert "image_result" not in types(events)
