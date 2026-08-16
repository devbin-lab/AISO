# -*- coding: utf-8 -*-
"""Image-result provenance regressions.

These cases deliberately keep the UI's prose and the backend's verified
``image_result`` event separate.  A model sentence is never proof that
ComfyUI produced an image card.
"""

from __future__ import annotations

import asyncio

import agent
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


def _generated_result() -> dict:
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
            "seed": "42",
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "cfg": 5,
            "sampler": "euler_ancestral",
            "scheduler": "normal",
        },
    }


def test_feedback_without_a_verified_image_result_cannot_publish_generation_complete_claim(env):
    """A free-form feedback turn must not invent a prior ComfyUI completion.

    This reproduces the observed case: the user only comments on expression,
    but the model answers with the deterministic-looking completion copy even
    though no ``generate_image`` call or image card was emitted.
    """
    hallucinated_completion = (
        "이미지 생성을 완료했습니다. 결과 카드에서 이미지와 실제 ComfyUI 노드 워크플로를 확인할 수 있습니다.\n"
        "모델 Animagine-XL-4.0, seed 1523987604321, 크기 1024x1024\n"
        "실제 프롬프트: 1girl, fictional character"
    )
    events = env.run(
        FakeChat([{"content": hallucinated_completion}]),
        messages=[
            {
                "role": "user",
                "content": "표정이 너무 어두워. 내가 아는 에이메스는 더 밝은 아이돌 같은 표정이야.",
            }
        ],
        workspace="",
    )

    assert "image_result" not in types(events)
    delivered = "\n".join(event.get("text", "") for event in events if event["type"] == "content")
    assert "이미지 생성을 완료했습니다" not in delivered
    assert "결과 카드" not in delivered
    assert "실제 프롬프트" not in delivered


def test_split_completion_claim_is_held_until_the_provenance_guard(env, monkeypatch):
    """Provider chunks cannot split a fabricated completion around the guard."""
    hallucinated_completion = (
        "이미지 생성을 완료했습니다. 결과 카드에서 이미지와 실제 ComfyUI 노드 워크플로를 확인할 수 있습니다.\n"
        "모델 Animagine-XL-4.0, seed 42\n실제 프롬프트: 1girl"
    )

    async def split_completion(_host, _payload):
        yield {"type": "content", "text": "이미"}
        yield {"type": "content", "text": hallucinated_completion[2:]}
        yield {
            "_final": True,
            "content": hallucinated_completion,
            "thinking": "",
            "tool_calls": [],
            "done_reason": "stop",
        }

    async def collect():
        return [
            event async for event in agent.run_agent(
                host="h",
                workspace="",
                model="m",
                messages=[{"role": "user", "content": "표정이 너무 어두워. 더 밝은 표정이야."}],
            )
        ]

    monkeypatch.setattr(agent, "_chat_turn", split_completion)
    events = asyncio.run(collect())
    delivered = "\n".join(event.get("text", "") for event in events if event["type"] == "content")
    assert "이미지 생성을 완료했습니다" not in delivered
    assert "결과 카드" not in delivered
    assert "실제 프롬프트" not in delivered
    assert "이미지 생성 도구가 완료되지 않아" in delivered


def test_visual_correction_requires_verified_image_context():
    """A prose-only completion cannot turn feedback into a new GPU request."""
    completion_prose = (
        "이미지 생성을 완료했습니다. 결과 카드에서 확인하세요. "
        "실제 프롬프트: 1girl, fictional character"
    )
    feedback = "표정이 너무 어두워. 내가 아는 에이메스는 더 밝은 아이돌 같은 표정이야."

    assert not agent._looks_like_image_generation_request(
        feedback,
        completion_prose,
        previous_image_verified=False,
    )
    assert agent._looks_like_image_generation_request(
        feedback,
        completion_prose,
        previous_image_verified=True,
    )


def test_verified_image_result_is_bound_to_its_assistant_turn(env, monkeypatch):
    """Late events from an old retry must not be attachable to a newer turn."""

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**_kwargs):
        return _generated_result()

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    events = env.run(
        FakeChat([{"calls": [("generate_image", {"prompt": "1girl, original character"})]}]),
        messages=[{"role": "user", "content": "애니메이션 캐릭터 이미지를 생성해줘."}],
        workspace="",
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
        approval_mode="auto",
        assistant_turn_id="turn-current",
    )

    assert "image_result" in types(events), events
    image_event = next(event for event in events if event.get("type") == "image_result")
    # Tool events may use a response-step suffix, but an image card belongs to
    # the UI's active *run* ID.  This exact value lets the renderer reject a
    # delayed event from an old retry/conversation before it reaches state.
    assert image_event["assistantTurnId"] == "turn-current"
