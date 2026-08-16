from __future__ import annotations

import asyncio

from agent_approval import ApprovalRegistry


def test_primary_and_legacy_keys_share_one_decision() -> None:
    async def scenario() -> None:
        approvals = ApprovalRegistry()
        approvals.open("session:approval", "session:legacy")

        assert approvals.pending_count == 1
        assert approvals.resolve("session:legacy", True)
        assert await approvals.wait("session:approval", 0.1) is True

        approvals.close("session:approval", "session:legacy")
        assert approvals.pending_count == 0
        assert not approvals.resolve("session:approval", True)

    asyncio.run(scenario())


def test_timeout_is_a_rejection_without_leaking_pending_entry() -> None:
    async def scenario() -> None:
        approvals = ApprovalRegistry()
        approvals.open("s:a", "s:l")

        assert await approvals.wait("s:a", 0.001) is False
        approvals.close("s:a", "s:l")
        assert approvals.pending_count == 0

    asyncio.run(scenario())
