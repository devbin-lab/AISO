# -*- coding: utf-8 -*-
"""모델에게 나가는 영문 스키마 설명의 계약.

`_FIELD_DESCRIPTIONS`는 파라미터 '이름'만으로 설명을 고르는 평면 맵이라, 같은 이름이
도구마다 다른 뜻을 가지면 나중 리터럴이 조용히 이긴다. 실제로 `kind`가 그랬다:
list_mydb_library(코어/파일 종류)가 discord_schedule_add(메시지/브리핑)의 설명을
받아갔다. 파이썬은 dict 리터럴 중복 키를 오류로 알려주지 않으므로 테스트로 고정한다.
"""
from __future__ import annotations

import ast
import collections
import json
from pathlib import Path

import pytest

import agent
import tool_schema_language as tsl
from toolspec import REGISTRY

_SOURCE = Path(tsl.__file__)
_MODEL_SNAPSHOT = Path(__file__).parent / "_model_tools_snapshot.json"


def _literal_keys(map_name: str) -> list[str]:
    """소스의 dict 리터럴에서 키를 순서대로 뽑는다(중복이 사라지기 전에)."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = getattr(node, "target", None)
        if isinstance(node, ast.AnnAssign) and getattr(target, "id", "") == map_name:
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError(f"{map_name} 리터럴을 찾지 못했다")


@pytest.mark.parametrize(
    "map_name", ["_FUNCTION_DESCRIPTIONS", "_FIELD_DESCRIPTIONS", "_FUNCTION_FIELD_DESCRIPTIONS"]
)
def test_description_maps_have_no_duplicate_keys(map_name):
    keys = _literal_keys(map_name)
    duplicates = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not duplicates, (
        f"{map_name}에 중복 키 {duplicates} — 뒤에 온 값이 조용히 이긴다. "
        "도구마다 뜻이 다른 파라미터라면 _FUNCTION_FIELD_DESCRIPTIONS로 옮겨라."
    )


def _model_description(tool_name: str, param: str) -> str:
    for spec in REGISTRY.values():
        schema = getattr(spec, "schema", None) or (
            spec.get("schema") if isinstance(spec, dict) else None
        )
        if not isinstance(schema, dict):
            continue
        function = schema.get("function")
        if not isinstance(function, dict) or function.get("name") != tool_name:
            continue
        localized = tsl.model_schema_for(schema)
        return localized["function"]["parameters"]["properties"][param]["description"]
    raise AssertionError(f"{tool_name} 도구를 REGISTRY에서 찾지 못했다")


def test_kind_parameter_is_disambiguated_per_tool():
    """같은 `kind`라도 도구별로 자기 뜻을 받아야 한다."""
    library = _model_description("list_mydb_library", "kind")
    schedule = _model_description("discord_schedule_add", "kind")

    assert library != schedule, "두 도구의 kind 설명이 같다 — 평면 맵이 다시 뭉갠 것"
    assert "My DB" in library, f"list_mydb_library.kind 설명이 My DB 뜻이 아니다: {library!r}"
    assert "Schedule" in schedule, f"discord_schedule_add.kind 설명이 일정 뜻이 아니다: {schedule!r}"


def test_model_tools_snapshot():
    """모델에게 실제로 나가는 스키마 전문을 프리즈한다.

    `_agent_tools_snapshot.json`은 **한국어 UI 스키마**라서, 영문 설명이 엉뚱한 뜻으로
    바뀌어도 통과한다. 실제로 그 사각지대에서 8건이 틀린 채 나가고 있었다 —
    `glob.pattern`이 "Regular expression..."(grep 설명), `run_web.steps`가
    "Complete ordered plan steps."(update_plan 설명), `web_search.count`가
    "Number of repetitions." 등.

    설명 문구를 바꾸는 것은 모델 동작에 영향을 주는 계약 변경이다. 의도한 변경이면
    이 스냅샷을 함께 갱신하고, 그 diff가 리뷰 대상이 된다.
    """
    expected = json.loads(_MODEL_SNAPSHOT.read_text(encoding="utf-8"))
    assert agent.MODEL_AGENT_TOOLS == expected


def test_same_parameter_name_can_carry_different_meanings_in_one_tool():
    """한 도구 안에서 깊이가 다른 동명 파라미터가 각자 뜻을 갖는다.

    run_web의 최상위 `path`는 검증할 HTML 파일이고, `steps[].path`는 페이지 상태를
    읽는 점 표기 경로다. 둘 다 "Workspace-relative path."로 나가면 모델이 상태 단언에
    파일 경로를 넣는다.
    """
    schema = next(
        tool for tool in agent.MODEL_AGENT_TOOLS
        if tool["function"]["name"] == "run_web"
    )["function"]["parameters"]["properties"]

    file_path = schema["path"]["description"]
    state_path = schema["steps"]["items"]["properties"]["path"]["description"]

    assert file_path != state_path
    assert "not a file path" in state_path, state_path


def test_localized_descriptions_are_english_only():
    """한글 설명이 모델 쪽 스키마로 새지 않는다(원 계약 유지 확인)."""
    leaked: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            description = node.get("description")
            if isinstance(description, str) and tsl._HANGUL_RE.search(description):
                leaked.append(f"{path}: {description[:40]}")
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    for spec in REGISTRY.values():
        schema = getattr(spec, "schema", None) or (
            spec.get("schema") if isinstance(spec, dict) else None
        )
        if isinstance(schema, dict):
            walk(tsl.model_schema_for(schema), schema.get("function", {}).get("name", "?"))

    assert not leaked, f"모델 스키마에 한글 설명이 남았다: {leaked[:5]}"
