"""Single-process approval coordination for agent tool calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class PendingApproval:
    event: asyncio.Event
    approved: bool = False


class ApprovalRegistry:
    """Map current and legacy UI request keys to one approval decision."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}

    def open(self, primary_key: str, legacy_key: str) -> None:
        pending = PendingApproval(event=asyncio.Event())
        self._pending[primary_key] = pending
        self._pending[legacy_key] = pending

    async def wait(self, primary_key: str, timeout: float) -> bool:
        pending = self._pending.get(primary_key)
        if pending is None:
            return False
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return pending.approved

    def resolve(self, key: str, approved: bool) -> bool:
        pending = self._pending.get(key)
        if pending is None:
            return False
        pending.approved = approved
        pending.event.set()
        return True

    def close(self, *keys: str) -> None:
        for key in keys:
            self._pending.pop(key, None)

    @property
    def pending_count(self) -> int:
        return len({id(pending) for pending in self._pending.values()})
