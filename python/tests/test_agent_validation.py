from __future__ import annotations

from pathlib import Path

import agent_validation as validation


def test_dependency_contract_tracks_english_and_korean_file_lists() -> None:
    english = "Edit app.js and style.css, then validate a.html."
    korean = "app.js와 style.css를 수정한 뒤 a.html을 검증해줘"

    for request in (english, korean):
        assert validation.request_directly_mutates_dependency_path(request, "app.js")
        assert validation.request_directly_mutates_dependency_path(request, "style.css")


def test_preserved_path_rejects_case_alias_on_windows_contract() -> None:
    request = 'Do not edit "my app.js"; edit app.js, then validate a.html.'

    assert validation.request_explicitly_preserves_path(request, "my app.js")
    assert not validation.request_directly_mutates_dependency_path(request, "my app.js")


def test_move_into_existing_directory_reports_both_effect_paths(tmp_path: Path) -> None:
    (tmp_path / "backup").mkdir()

    assert validation.relative_tool_effect_paths(
        "move", {"src": "src/a.html", "dst": "backup"}, tmp_path
    ) == ["src/a.html", "backup/a.html"]


def test_workspace_effect_covers_symlink_resolved_descendant(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    protected = real / "a.html"
    protected.write_text("safe", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        # Symlink creation can be disabled by the Windows security policy.
        return

    assert validation.workspace_effect_covers_path(tmp_path, "alias", "real/a.html")
