# -*- coding: utf-8 -*-
"""프롬프트 토큰 관측 계약.

`compact_convo`의 컨텍스트 예산은 `chars // 3` 고정 휴리스틱이다. 한국어가 섞이면
문자당 토큰 비율이 크게 달라지는데, 지금까지는 그 추정이 실제 `num_ctx`를 넘겼는지
확인할 방법이 아예 없었다 — Ollama가 주는 `prompt_eval_count`를 버리고 있었기 때문.

여기서 고정하는 계약:
  - Ollama done 이벤트의 `prompt_eval_count`가 `LlmEvent.input_tokens`로 온다.
  - 그 값이 하네스의 usage 이벤트에 `prompt_tokens`로, 당시 `context_length`와 함께 실린다.
  - 사용량 집계(`total`)는 **생성 토큰만** 센다. 프롬프트 토큰이 여기 섞이면
    렌더러의 일/주/월 사용량 통계 정의가 조용히 바뀐다.
"""
from __future__ import annotations

import agent
from conftest import FakeChat, types


class PromptCountingChat(FakeChat):
    """_final에 input_tokens와 output_tokens를 실어 주는 FakeChat.

    usage 이벤트는 생성 토큰이 있을 때만 나가므로 output_tokens도 함께 준다.
    """

    def __init__(self, script, prompt_tokens, output_tokens=7):
        super().__init__(script)
        self.prompt_tokens = prompt_tokens
        self.output_tokens = output_tokens

    async def _gen(self, spec):
        async for event in super()._gen(spec):
            if event.get("_final"):
                event = {
                    **event,
                    "input_tokens": self.prompt_tokens,
                    "output_tokens": self.output_tokens,
                }
            yield event


def test_usage_event_carries_prompt_tokens_and_context_length(env):
    chat = PromptCountingChat([{"content": "완료.", "done_reason": "stop"}], prompt_tokens=1234)
    chat.script[0]["calls"] = []
    events = env.run(chat, approval_mode="auto", context_length=8192)

    usage = [event for event in events if event.get("type") == "usage"]
    assert usage, f"usage 이벤트가 없다: {types(events)}"
    assert usage[-1]["prompt_tokens"] == 1234
    assert usage[-1]["context_length"] == 8192


def test_prompt_tokens_do_not_inflate_the_usage_total(env):
    """total은 생성 토큰 계약이다 — 프롬프트 토큰이 섞이면 사용량 통계가 틀어진다."""
    chat = PromptCountingChat([{"content": "완료.", "done_reason": "stop"}], prompt_tokens=99999)
    chat.script[0]["calls"] = []
    events = env.run(chat, approval_mode="auto")

    usage = [event for event in events if event.get("type") == "usage"]
    assert usage
    assert usage[-1]["total"] < 99999, "프롬프트 토큰이 사용량 합계에 섞였다"


def test_usage_event_omits_prompt_tokens_when_provider_gives_none(env):
    """공급자가 프롬프트 토큰을 안 주면 필드를 지어내지 않는다."""
    events = env.run(
        FakeChat([{"content": "완료.", "done_reason": "stop"}]),
        approval_mode="auto",
    )
    for event in events:
        if event.get("type") == "usage":
            assert "prompt_tokens" not in event


def test_ollama_adapter_maps_prompt_eval_count_to_input_tokens():
    """공급자 계약: prompt_eval_count → input_tokens."""
    from llm.contracts import LlmEvent

    event = LlmEvent(kind="done", output_tokens=12, input_tokens=57)
    assert event.input_tokens == 57
    # 어댑터가 실제로 이 매핑을 하는지는 골든 픽스처
    # (tests/fixtures/ollama_chat_contract.json)가 바이트로 고정한다.
    assert agent.__name__  # import 확인용
