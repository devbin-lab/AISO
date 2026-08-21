# -*- coding: utf-8 -*-
"""agent_runner의 협력자는 정적으로 해석 가능해야 한다.

예전에는 `bind_dependencies(globals())`가 런 시작마다 agent 모듈의 전역 107개를
agent_runner의 globals()에 복사했다. 동작은 했지만, 그 3,300여 줄 안의 이름이
**소스 어디에도 정의돼 있지 않다.** mypy·IDE·린터가 하나도 해석하지 못하고,
이름 오타는 그 코드 경로가 실제로 실행될 때까지 드러나지 않는다.

이제 러너가 `import agent as deps`로 직접 읽는다. 호출 시점 속성 조회이므로
`monkeypatch.setattr(agent, ...)` 시임(테스트 122곳)은 그대로 동작한다.

여기서 고정하는 계약:
  - 런타임 전역 주입이 다시 들어오지 않는다.
  - `deps.X`로 참조하는 이름은 전부 agent 모듈에 실제로 존재한다.
  - 러너에 정의도 import도 되지 않은 자유 이름이 남아 있지 않다.

세 번째가 핵심이다. 누군가 주입 방식으로 되돌리면 자유 이름이 다시 생기고,
그 순간 이 테스트가 실패한다.
"""
from __future__ import annotations

import ast
import builtins
import io
import re
from pathlib import Path

import agent
import agent_runner

_SOURCE = Path(agent_runner.__file__)


def _tree() -> ast.Module:
    return ast.parse(io.open(_SOURCE, encoding="utf-8").read())


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def _function_local_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                names.add(sub.id)
            elif isinstance(sub, ast.arg):
                names.add(sub.arg)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(sub.name)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in sub.names)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                names.add(sub.name)
            elif isinstance(sub, ast.withitem) and isinstance(sub.optional_vars, ast.Name):
                names.add(sub.optional_vars.id)
    return names


def test_runtime_global_injection_is_gone():
    assert not hasattr(agent_runner, "bind_dependencies"), (
        "런타임 전역 주입이 되살아났다 — 러너의 이름이 다시 정적으로 해석 불가능해진다."
    )
    assert "bind_dependencies" not in getattr(agent_runner, "__all__", [])


def test_no_unresolved_free_names_remain():
    """정의도 import도 되지 않은 이름이 남아 있으면 그건 다시 주입 의존이다."""
    tree = _tree()
    known = _module_level_names(tree) | _function_local_names(tree) | set(dir(builtins))
    free = sorted({
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id not in known
        and not node.id.startswith("__")
    })
    assert not free, f"주입에 의존하는 자유 이름이 남아 있다: {free[:15]}"


def test_every_facade_reference_actually_exists():
    """`deps.X`가 가리키는 이름이 실제로 agent 모듈에 있다.

    오타는 예전이라면 그 코드 경로가 실행될 때까지 숨어 있었다.
    """
    source = io.open(_SOURCE, encoding="utf-8").read()
    # 주석/문서화 문자열의 예시(deps.X)는 제외한다.
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines()
    )
    referenced = sorted(set(re.findall(r"\bdeps\.([A-Za-z_][A-Za-z0-9_]*)\b", code_only)))
    assert len(referenced) > 50, f"참조를 제대로 수집하지 못했다: {len(referenced)}개"

    missing = [name for name in referenced if not hasattr(agent, name)]
    assert not missing, f"agent 모듈에 없는 이름을 참조한다: {missing}"


def test_facade_monkeypatch_seam_still_works(monkeypatch):
    """시임 보존 확인 — 테스트 122곳이 의존하는 성질이다."""
    sentinel = object()
    monkeypatch.setattr(agent, "MAX_STEPS", sentinel)
    assert agent_runner.deps.MAX_STEPS is sentinel, (
        "monkeypatch.setattr(agent, ...)가 러너에 반영되지 않는다 — 기존 시임이 깨졌다."
    )
