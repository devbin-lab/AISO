from __future__ import annotations

import agent_prompting as prompting


def test_programming_prompt_reflects_only_exposed_tools() -> None:
    disabled = prompting.programming_policy_prompt(frozenset())
    enabled = prompting.programming_policy_prompt(
        frozenset({"write_code_file", "edit_code_file", "run_web"})
    )

    assert "Project code authoring and editing are disabled" in disabled
    assert "write_code_file, edit_code_file" in enabled
    assert "run_web" in enabled


def test_exact_tool_scope_never_claims_disabled_skills_or_web() -> None:
    text = prompting.exact_tool_scope_prompt(["read_file"])

    assert "Available tools: read_file." in text
    assert "Skill creation and execution are unavailable" in text
    assert "Web research is unavailable" in text


def test_saved_todo_instruction_never_requires_a_workspace() -> None:
    text = prompting.operational_tool_policy_prompt(frozenset({"list_calendar_events"}))

    assert "does not require a workspace" in text
    assert "do not use update_plan as a substitute" in text


def test_final_response_language_prompt_is_english_policy_with_dynamic_target() -> None:
    korean = prompting.final_response_language_prompt("ko")
    english = prompting.final_response_language_prompt("en")

    assert "Write every user-facing final answer in Korean" in korean
    assert "Write every user-facing final answer in English" in english
    assert "Do not translate tool names" in korean


def test_markdown_safe_plain_text_neutralises_links_and_markup() -> None:
    text = prompting.markdown_safe_plain_text("https://example.test <script> @x")

    assert "https-colon-slash-slash" in text
    assert "&lt;script&gt;" in text
    assert "-at-x" in text
