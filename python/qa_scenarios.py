"""Small deterministic regression pack for user-visible Aiso workflows.

It deliberately does not spend model tokens.  These checks protect the
contracts that local and cloud models rely on: correct routing, evidence
retention, document extraction, and ToDo persistence.  Model quality can be
evaluated on top of this stable baseline without confusing a model failure
with an application regression.
"""

from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from document_todos import (
    analyze_documents,
    list_todos,
    save_todos,
    temporary_todo_database,
    update_todo,
)
from extract import extract_document_segments
from tools import ToolError, list_tree, validate_workspace


def _result(identifier: str, title: str, check: Callable[[], str]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        detail = check()
        status = "pass"
    except Exception as error:  # noqa: BLE001 - this is an assessment boundary
        status = "fail"
        detail = f"{type(error).__name__}: {error}"
    return {
        "id": identifier,
        "title": title,
        "status": status,
        "detail": detail,
        "durationMs": round((time.perf_counter() - started) * 1000),
    }


def run_scenario_pack() -> dict[str, Any]:
    """Run isolated, file-system based contracts and return a UI-safe report."""
    with tempfile.TemporaryDirectory(prefix="aiso-qa-") as temp:
        root = Path(temp)
        (root / "docs").mkdir()
        (root / "docs" / "plan.md").write_text(
            "# 개발 계획\n\n1. 핵심 게임플레이 시스템 구현\n2. 2026년 8월 23일 발표 자료 제출\n",
            encoding="utf-8",
        )

        def folder_structure_stays_a_file_tool() -> str:
            tree = list_tree(validate_workspace(str(root)), ".", max_depth=3)
            if "docs/" not in tree or "plan.md" not in tree:
                raise AssertionError("작업 폴더의 문서를 찾지 못했습니다.")
            return "파일 구조 조회는 문서 ToDo 분석 없이 작업 폴더 트리를 반환합니다."

        def evidence_candidate_contract() -> str:
            analysis = analyze_documents(str(root), ["docs/plan.md"])
            items = analysis["candidates"]
            if len(items) < 2:
                raise AssertionError("실행 동사가 있는 문서에서 ToDo 후보를 찾지 못했습니다.")
            evidence = items[0]["evidence"][0]
            if evidence["file"] != "docs/plan.md" or not evidence["location"] or not evidence["quote"]:
                raise AssertionError("후보에 클릭 가능한 원문 근거가 없습니다.")
            return f"{len(items)}개 후보가 파일·위치·원문 인용을 유지합니다."

        def persistence_contract() -> str:
            # QA must never add its fixture task to the user's central ToDo DB.
            with temporary_todo_database(root / ".aiso" / "qa-document-todos.sqlite3"):
                candidate = analyze_documents(str(root), ["docs/plan.md"])["candidates"][0]
                saved = save_todos(str(root), [candidate])
                item = saved["items"][0]
                updated = update_todo(item["id"], {"status": "done"})
                stored = list_todos(str(root))["items"]
            if updated["status"] != "done" or stored[0]["status"] != "done":
                raise AssertionError("ToDo 상태가 저장소에 반영되지 않았습니다.")
            return "원문 근거를 보존한 ToDo 저장·완료 상태 변경이 정상입니다."

        def powerpoint_location_contract() -> str:
            pptx = root / "slides.pptx"
            xml = (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>캐릭터 수익 모델 구축</a:t>'
                '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
            )
            with zipfile.ZipFile(pptx, "w") as archive:
                archive.writestr("ppt/slides/slide1.xml", xml)
            segments = extract_document_segments(pptx)
            if len(segments) != 1 or segments[0].location != "슬라이드 1" or "수익 모델" not in segments[0].text:
                raise AssertionError("PowerPoint 슬라이드 위치 또는 텍스트를 보존하지 못했습니다.")
            return "PPTX 텍스트를 슬라이드 번호와 함께 추출합니다."

        def outside_workspace_is_rejected() -> str:
            try:
                analyze_documents(str(root), ["../outside.pdf"])
            except ToolError:
                return "작업 폴더 밖 문서 경로가 차단됩니다."
            raise AssertionError("작업 폴더 밖 문서가 허용되었습니다.")

        cases = [
            _result("routing-folder-tree", "파일 구조 조회 라우팅", folder_structure_stays_a_file_tool),
            _result("evidence-todo", "원문 근거 ToDo 계약", evidence_candidate_contract),
            _result("todo-persistence", "ToDo 저장·상태 변경", persistence_contract),
            _result("pptx-location", "PPTX 슬라이드 근거 추출", powerpoint_location_contract),
            _result("workspace-boundary", "문서 경로 보안 경계", outside_workspace_is_rejected),
        ]
    passed = sum(1 for item in cases if item["status"] == "pass")
    return {
        "name": "Aiso 시나리오 QA 평가팩",
        "executedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {"passed": passed, "failed": len(cases) - passed, "total": len(cases)},
        "scenarios": cases,
        "scope": "문서·ToDo·작업 폴더 경계의 결정적 회귀 검사",
    }
