from __future__ import annotations

from response_language import (
    detect_response_language,
    response_language_from_messages,
    response_language_name,
)


def test_distinctive_scripts_are_detected_without_a_model_call() -> None:
    assert detect_response_language("현재 작업을 요약해줘") == "ko"
    assert detect_response_language("Summarize the current work.") == "en"
    assert detect_response_language("現在の作業を要約してください") == "ja"
    assert detect_response_language("请总结当前工作") == "zh"


def test_explicit_output_language_overrides_the_request_script() -> None:
    assert detect_response_language("현재 작업을 영어로 답변해줘") == "en"
    assert detect_response_language("Answer this in Korean: summarize the work") == "ko"
    assert detect_response_language("日本語で答えてください") == "ja"


def test_short_latin_follow_up_inherits_prior_substantive_user_language() -> None:
    assert response_language_from_messages([
        {"role": "user", "content": "이 문서를 요약해줘"},
        {"role": "assistant", "content": "알겠습니다."},
        {"role": "user", "content": "continue"},
    ]) == "ko"


def test_only_original_user_messages_control_the_selected_language() -> None:
    raw_messages = [
        {"role": "user", "content": "Please summarize the attachment."},
        {"role": "assistant", "content": "한국어로 된 도구 결과"},
    ]
    assert response_language_from_messages(raw_messages) == "en"
    assert response_language_name("ko") == "Korean"
