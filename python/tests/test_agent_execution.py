from __future__ import annotations

import agent_execution as execution


def test_auto_mode_never_requests_an_agent_approval() -> None:
    assert not execution.requires_approval(
        approval_mode="auto",
        approval_name="run_command",
        needs_approval_for_tool=lambda *_: True,
        workspace_context_exposed=True,
        is_network_egress=True,
    )


def test_read_mode_retains_local_and_workspace_egress_confirmation() -> None:
    assert execution.requires_approval(
        approval_mode="read",
        approval_name="run_command",
        needs_approval_for_tool=lambda *_: True,
        workspace_context_exposed=False,
        is_network_egress=False,
    )
    assert execution.requires_approval(
        approval_mode="read",
        approval_name="web_search",
        needs_approval_for_tool=lambda *_: False,
        workspace_context_exposed=True,
        is_network_egress=True,
    )


def test_parse_args_and_tool_call_normalisation_are_deterministic() -> None:
    assert execution._parse_args('{"q":"Aiso"}') == {"q": "Aiso"}
    calls = execution._normalize_tool_calls(
        [{"id": "tool-1", "function": {"name": "web_search", "arguments": '{"q":"Aiso"}'}}],
        "turn-1",
    )

    assert calls[0]["provider_tool_call_id"] == "tool-1"
    assert calls[0]["function"]["arguments"] == {"q": "Aiso"}
