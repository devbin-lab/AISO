"""Single-process approval coordination for agent tool calls.

승인 대기의 결과는 세 가지다 — 승인 / 거부 / **응답 없음**.

예전에는 `wait()`가 bool 하나만 돌려줬고, 타임아웃과 거부가 구분되지 않았다. 실행
판단으로는 옳다(응답이 없으면 실행하지 않는다 = fail-closed). 그러나 그 뒤가 문제였다:

  - 모델에게 "사용자가 이 작업을 승인하지 않았습니다"라고 전달했다. 사용자는 거부한
    적이 없다. 자리를 비웠거나 창을 닫았을 뿐이다. 12B 모델은 거부와 무응답에 다르게
    반응해야 하는데(거부=그 방향을 접는다 / 무응답=다시 물어볼 수 있다) 같은 신호를 받았다.
  - 원장에 `status="rejected", rejected=True`로 **영구 기록**했다. 원장은 '정확히 한 번'
    계약의 근거인데, 일어나지 않은 사용자 결정을 사실로 남긴 셈이다.

`wait_outcome()`은 셋을 구분해 돌려준다. `wait()`는 예전 bool 계약을 그대로 유지하므로
"응답이 없으면 실행하지 않는다"는 고정된 안전 계약은 변하지 않는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

# approved: 사용자가 승인 / rejected: 사용자가 거부 / expired: 응답이 오지 않음
ApprovalOutcome = Literal["approved", "rejected", "expired"]

APPROVED: ApprovalOutcome = "approved"
REJECTED: ApprovalOutcome = "rejected"
EXPIRED: ApprovalOutcome = "expired"


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

    async def wait_outcome(self, primary_key: str, timeout: float) -> ApprovalOutcome:
        """승인 결과를 세 값으로 구분해 돌려준다.

        키가 사라진 경우(백엔드 재시작 등으로 대기 항목이 없음)도 `expired`다 —
        사용자가 거부한 것이 아니라 결정이 도달하지 못한 것이기 때문이다.
        """
        pending = self._pending.get(primary_key)
        if pending is None:
            return EXPIRED
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return EXPIRED
        return APPROVED if pending.approved else REJECTED

    async def wait(self, primary_key: str, timeout: float) -> bool:
        """실행 판단용 bool — 승인되지 않은 모든 결과는 False다(fail-closed).

        이 계약은 의도적으로 유지한다. 무응답에 도구를 실행하면 안 된다.
        """
        return await self.wait_outcome(primary_key, timeout) == APPROVED

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
