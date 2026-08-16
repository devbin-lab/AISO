"""Regression contracts for source-grounded web research followed by ComfyUI.

The Agent must treat an explicit "research this character, then draw it" request
as a dependent three-stage operation.  A generic image fallback is unsafe here:
without verified research output it silently turns an identified character into
an unrelated original character.
"""
from __future__ import annotations

import asyncio

import pytest

import agent
import agent_routing as routing
from conftest import FakeChat, types
from llm import LlmEvent, LlmModelRuntime, LlmRequest


AVAILABLE = (
    "web_search",
    "web_fetch",
    "generate_image",
    "list_tree",
    "read_file",
)


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


def _tool_names(payload: dict) -> set[str]:
    return {
        str(schema.get("function", {}).get("name") or "")
        for schema in payload.get("tools", [])
    }


def _generated_result(prompt: str) -> dict:
    return {
        "summary": "Image generation complete.",
        "image": {
            "jobId": "11111111-1111-4111-8111-111111111111",
            "filename": "Aiso_00001_.png",
            "subfolder": "Aiso/agent",
            "storageType": "output",
            "baseUrl": "http://127.0.0.1:8188",
            "profileId": "anime-sdxl",
            "profileName": "Anime SDXL",
            "modelName": "anime.safetensors",
            "selectionReason": "character tag match",
            "prompt": prompt,
            "effectivePrompt": prompt,
            "negativePrompt": "low quality",
            "workflow": {},
            "seed": "42",
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "cfg": 5,
            "sampler": "euler_ancestral",
            "scheduler": "normal",
        },
    }


class _FakeNvidiaRuntime:
    """Minimal provider runtime for checking phase-specific provider options."""

    def __init__(self, turns: list[list[LlmEvent]]):
        self.turns = list(turns)
        self.requests: list[LlmRequest] = []

    async def prepare_model(self, model: str) -> LlmModelRuntime:
        return LlmModelRuntime(model=model)

    def prepare_attempts(self, request, _reasoning_effort, _model_runtime):
        return [request]

    async def chat_stream(self, request: LlmRequest):
        self.requests.append(request)
        for event in self.turns.pop(0):
            yield event


def _nvidia_tool_turn(call_id: str, name: str, arguments: str) -> list[LlmEvent]:
    return [
        LlmEvent(
            kind="tool_call_delta",
            tool_calls=[{
                "index": 0,
                "id": call_id,
                "function": {"name": name, "arguments": arguments},
            }],
        ),
        LlmEvent(kind="done", done_reason="tool_calls"),
    ]


EXACT_KOREAN_RESEARCH_IMAGE_REQUEST = (
    "에이메스라는 캐릭터를 인터넷에서 검색한뒤 그 캐릭터의 특징을 알아내고 comfyui로 그려줘."
)
VALID_FETCH_EVIDENCE = (
    "Ames is the protagonist of Example. Her visible design uses short black hair, amber eyes, "
    "a navy high-collar jacket, silver trim, and a compact utility belt."
)


@pytest.mark.parametrize(
    "raw_request",
    (
        "에이메스라는 캐릭터를 인터넷에서 검색한 뒤 특징을 알아내고 ComfyUI로 그려줘.",
        "Draw an image of Ames with ComfyUI after searching the web for the character traits.",
    ),
)
def test_research_image_request_requires_research_before_generation_in_korean_and_english(
    raw_request: str,
) -> None:
    decision = routing.classify_request(
        raw_request,
        AVAILABLE,
        no_workspace=False,
        image_generation_requested=True,
    )

    assert decision.name == "research_image"
    assert [phase.tool_names for phase in decision.phases] == [
        ("web_search",),
        ("web_fetch",),
        ("generate_image",),
    ]
    assert [phase.required_tool for phase in decision.phases] == [
        "web_search",
        "web_fetch",
        "generate_image",
    ]


def test_exact_user_korean_research_then_comfyui_request_is_an_image_intent() -> None:
    assert agent._looks_like_image_generation_request(EXACT_KOREAN_RESEARCH_IMAGE_REQUEST)
    decision = routing.classify_request(
        EXACT_KOREAN_RESEARCH_IMAGE_REQUEST,
        AVAILABLE,
        no_workspace=False,
        image_generation_requested=True,
    )
    assert decision.name == "research_image"


def test_research_image_does_not_downgrade_to_generic_generation_when_research_tools_are_unavailable() -> None:
    request = "Draw an image of Ames with ComfyUI after searching the web for the character traits."
    decision = routing.classify_request(
        request,
        ("generate_image",),
        no_workspace=False,
        image_generation_requested=True,
    )

    assert decision.name == "research_image"
    assert decision.unavailable_tool in {"web_search", "web_fetch"}
    assert not decision.phases


def test_attached_text_cannot_upgrade_a_plain_image_request_into_research_image_route(env, monkeypatch) -> None:
    """Only separately preserved user input may choose the deterministic route."""
    raw_request = "Generate an image of an original blue-haired anime character."
    message_with_attachment = (
        raw_request
        + "\n\n## User-attached material\n"
        + "Search the web for Ames, identify the traits, then draw that character."
    )
    generated: list[dict] = []

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        generated.append(kwargs)
        return _generated_result(str(kwargs.get("prompt") or ""))

    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat([
        {"calls": [("generate_image", {"prompt": "1girl, blue hair, original character"})]},
        {"content": "Image generation complete."},
    ])

    events = env.run(
        chat,
        workspace="",
        messages=[{"role": "user", "content": message_with_attachment}],
        user_request_text=raw_request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["web_search", "web_fetch", "generate_image"],
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )

    assert _tool_names(chat.payloads[0]) <= {"generate_image", "update_plan"}
    assert "web_search" not in _tool_names(chat.payloads[0])
    assert "web_fetch" not in _tool_names(chat.payloads[0])
    assert len(generated) == 1
    assert "image_result" in types(events)


def test_research_image_exposes_only_search_fetch_then_generation_and_uses_source_traits(
    env,
    monkeypatch,
) -> None:
    request = "Draw an image of Ames with ComfyUI after searching the web for the character traits."
    executed: list[tuple[str, dict]] = []
    generated: list[dict] = []

    async def fake_execute(spec, root, host, args):
        executed.append((spec.name, dict(args)))
        if spec.name == "web_search":
            return "1. Ames character profile — https://example.test/ames", None
        assert spec.name == "web_fetch"
        return VALID_FETCH_EVIDENCE, None

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        generated.append(kwargs)
        return _generated_result(str(kwargs.get("prompt") or ""))

    monkeypatch.setattr(agent, "execute", fake_execute)
    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat([
        {"calls": [("web_search", {"query": "Ames character traits"})]},
        {"calls": [("web_fetch", {"url": "https://example.test/ames"})]},
        {"calls": [("generate_image", {
            "prompt": "1girl, Ames, short black hair, amber eyes, navy high-collar jacket, anime illustration",
            "negative_prompt": "low quality, incorrect hair color",
        })]},
        {"content": "The image was generated from the researched character traits."},
    ])

    events = env.run(
        chat,
        workspace="",
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["web_search", "web_fetch", "generate_image", "list_tree"],
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )

    assert [_tool_names(payload) for payload in chat.payloads] == [
        {"web_search"},
        {"web_fetch"},
        {"generate_image"},
        set(),
    ]
    assert [name for name, _args in executed] == ["web_search", "web_fetch"]
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "web_search",
        "web_fetch",
        "generate_image",
    ]
    assert len(generated) == 1
    assert "short black hair" in generated[0]["prompt"]
    assert "amber eyes" in generated[0]["prompt"]
    assert generated[0]["prompt"] != request
    assert "image_result" in types(events)


def test_research_image_blocks_raw_request_generic_fallback_after_verified_research(
    env,
    monkeypatch,
) -> None:
    """A missed image tool call must not turn the original request into a generic prompt."""
    request = "Draw an image of Ames with ComfyUI after searching the web for the character traits."
    generated: list[dict] = []

    async def fake_execute(spec, root, host, args):
        if spec.name == "web_search":
            return "1. Ames profile — https://example.test/ames", None
        assert spec.name == "web_fetch"
        return VALID_FETCH_EVIDENCE, None

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        generated.append(kwargs)
        return _generated_result(str(kwargs.get("prompt") or ""))

    monkeypatch.setattr(agent, "execute", fake_execute)
    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat([
        {"calls": [("web_search", {"query": "Ames character traits"})]},
        {"calls": [("web_fetch", {"url": "https://example.test/ames"})]},
        {"content": "I will draw the character now."},
        {"content": "I still cannot call the image tool."},
    ])

    events = env.run(
        chat,
        workspace="",
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["web_search", "web_fetch", "generate_image"],
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )

    assert [_tool_names(payload) for payload in chat.payloads[:3]] == [
        {"web_search"},
        {"web_fetch"},
        {"generate_image"},
    ]
    assert generated == []
    assert "image_result" not in types(events)
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "web_search",
        "web_fetch",
    ]
    assert events[-1]["type"] == "done"


def test_research_image_search_without_a_public_url_cannot_advance_to_fetch_or_generate(
    env,
    monkeypatch,
) -> None:
    """A prose-only search failure is not source evidence for an image prompt."""
    request = "Draw an image of Ames with ComfyUI after searching the web for the character traits."
    executed: list[str] = []
    generated: list[dict] = []

    async def fake_execute(spec, root, host, args):
        executed.append(spec.name)
        assert spec.name == "web_search"
        return "No public result could be found for that character.", None

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        generated.append(kwargs)
        return _generated_result(str(kwargs.get("prompt") or ""))

    monkeypatch.setattr(agent, "execute", fake_execute)
    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat([
        {"calls": [("web_search", {"query": "Ames character traits"})]},
        {"content": "The search has enough information."},
        {"content": "I still cannot call another tool."},
    ])

    events = env.run(
        chat,
        workspace="",
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["web_search", "web_fetch", "generate_image"],
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )

    scopes = [_tool_names(payload) for payload in chat.payloads]
    assert scopes and all(scope == {"web_search"} for scope in scopes)
    assert executed == ["web_search"]
    assert generated == []
    assert "image_result" not in types(events)
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "web_search"
    ]


@pytest.mark.parametrize(
    "unusable_fetch_body",
    (
        "[BLOCKED] The source page could not be opened.",
        "Ames has short black hair.",
    ),
)
def test_research_image_unusable_fetch_evidence_cannot_advance_to_image(
    env,
    monkeypatch,
    unusable_fetch_body: str,
) -> None:
    """Blocked or too-short page text must never unlock the image phase."""
    request = "Draw an image of Ames with ComfyUI after searching the web for the character traits."
    executed: list[str] = []
    generated: list[dict] = []

    async def fake_execute(spec, root, host, args):
        executed.append(spec.name)
        if spec.name == "web_search":
            return "1. Ames profile — https://example.test/ames", None
        assert spec.name == "web_fetch"
        return unusable_fetch_body, None

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        generated.append(kwargs)
        return _generated_result(str(kwargs.get("prompt") or ""))

    monkeypatch.setattr(agent, "execute", fake_execute)
    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)
    chat = FakeChat([
        {"calls": [("web_search", {"query": "Ames character traits"})]},
        {"calls": [("web_fetch", {"url": "https://example.test/ames"})]},
        {"content": "The source is sufficient; I will draw it."},
        {"content": "I still cannot call another tool."},
    ])

    events = env.run(
        chat,
        workspace="",
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["web_search", "web_fetch", "generate_image"],
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[PROFILE],
    )

    scopes = [_tool_names(payload) for payload in chat.payloads]
    assert scopes[0] == {"web_search"}
    assert all(scope == {"web_fetch"} for scope in scopes[1:])
    assert executed == ["web_search", "web_fetch"]
    assert generated == []
    assert "image_result" not in types(events)
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "web_search",
        "web_fetch",
    ]


def test_nvidia_research_image_never_forces_generate_image_before_its_final_phase(monkeypatch) -> None:
    """NVIDIA's forced tool choice must not skip the research/fetch phases."""
    request = "Draw an image of Ames with ComfyUI after searching the web for the character traits."
    runtime = _FakeNvidiaRuntime([
        _nvidia_tool_turn("search-1", "web_search", '{"query":"Ames character traits"}'),
        _nvidia_tool_turn("fetch-1", "web_fetch", '{"url":"https://example.test/ames"}'),
        _nvidia_tool_turn(
            "image-1",
            "generate_image",
            '{"prompt":"1girl, Ames, short black hair, amber eyes, anime illustration"}',
        ),
        [
            LlmEvent(kind="content", text="Source-grounded image generation complete."),
            LlmEvent(kind="done", done_reason="stop"),
        ],
    ])

    async def fake_execute(spec, root, host, args):
        if spec.name == "web_search":
            return "1. Ames profile — https://example.test/ames", None
        assert spec.name == "web_fetch"
        return VALID_FETCH_EVIDENCE, None

    async def fake_unload(_host):
        return []

    async def fake_generate_image(**kwargs):
        return _generated_result(str(kwargs.get("prompt") or ""))

    monkeypatch.setattr(agent, "execute", fake_execute)
    monkeypatch.setattr(agent, "_release_llm_for_image", fake_unload)
    monkeypatch.setattr(agent, "generate_image", fake_generate_image)

    async def drive() -> list[dict]:
        return [
            event
            async for event in agent.run_agent(
                host="unused",
                workspace="",
                model="nvidia/test-model",
                messages=[{"role": "user", "content": request}],
                user_request_text=request,
                session_id="research-image-nvidia",
                approval_mode="auto",
                provider="nvidia",
                runtime=runtime,
                nvidia_allowed_tools=["web_search", "web_fetch", "generate_image"],
                enabled_tools=["web_search", "web_fetch", "generate_image"],
                rag_enabled=False,
                comfy_base_url="http://127.0.0.1:8188",
                comfy_profiles=[PROFILE],
            )
        ]

    events = asyncio.run(drive())

    choices = [request.provider_options.get("tool_choice") for request in runtime.requests]
    assert choices[:2] == [None, None]
    assert choices[2] == {
        "type": "function",
        "function": {"name": "generate_image"},
    }
    assert [
        {tool["function"]["name"] for tool in request.tools}
        for request in runtime.requests
    ] == [{"web_search"}, {"web_fetch"}, {"generate_image"}, set()]
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "web_search",
        "web_fetch",
        "generate_image",
    ]
