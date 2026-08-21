"""My DB 에 대한 에이전트 툴 표면 — 삭제 능력이 없어야 한다.

사용자가 2026-07-19 에 못 박은 경계다:
    "DB에 뭐가 있는지 조회하는것도 삭제는 못했으면 좋겠고 복구하는 거는 가능 했으면 좋겠고."

사용자 UI 에는 휴지통 완전 삭제가 생겼다. 그 능력이 에이전트 쪽으로 새지 않아야 한다.
툴을 추가하면 REGISTRY 한 줄이면 되므로, 실수는 조용히 일어난다 — 그래서 여기서 잠근다.
"""

from __future__ import annotations

import mydb_agent
import toolspec


MYDB_TOOL_NAMES = {name for name in toolspec.REGISTRY if "mydb" in name}

# 허용된 전부. 새 툴을 추가하려면 이 목록을 의도적으로 고쳐야 한다.
ALLOWED = {
    "list_mydb_library",
    "list_mydb_history",
    "list_mydb_trash",
    "restore_mydb_trash_node",
}

DESTRUCTIVE_WORDS = ("purge", "delete", "remove", "destroy", "clear", "wipe", "erase", "drop")


def test_mydb_tool_surface_is_exactly_the_allowed_set():
    assert MYDB_TOOL_NAMES == ALLOWED, (
        f"My DB 에이전트 툴 표면이 바뀌었다: {MYDB_TOOL_NAMES ^ ALLOWED}"
    )


def test_no_mydb_tool_name_suggests_destruction():
    for name in MYDB_TOOL_NAMES:
        for word in DESTRUCTIVE_WORDS:
            assert word not in name, f"파괴적 이름의 My DB 툴이 등록됐다: {name}"


def test_mydb_module_exposes_no_destructive_handler():
    # 스키마가 아니라 **모듈의 공개 함수**를 본다. 핸들러가 먼저 생기고
    # 나중에 등록되는 순서로도 경계가 열릴 수 있기 때문이다.
    public = [n for n in dir(mydb_agent) if not n.startswith("_") and callable(getattr(mydb_agent, n))]
    for name in public:
        for word in DESTRUCTIVE_WORDS:
            assert word not in name.lower(), f"mydb_agent 에 파괴적 함수가 생겼다: {name}"


def test_mydb_tools_never_carry_a_delete_grade():
    # restore 는 DESTRUCTIVE(쓰기) 등급이지만 DELETE 여서는 안 된다.
    for name in MYDB_TOOL_NAMES:
        spec = toolspec.REGISTRY[name]
        assert spec.approval is not toolspec.Approval.DELETE, (
            f"{name} 이 삭제 등급으로 등록됐다 — My DB 는 에이전트에게 삭제를 허용하지 않는다"
        )
