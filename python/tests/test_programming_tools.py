from __future__ import annotations

import pytest

import tools
from tools import ToolError


def test_code_tools_write_edit_and_multi_edit_utf8_project_files(tmp_path):
    made = tools.write_code_file(
        tmp_path,
        "src/app.py",
        "def greet():\n    return '안녕'\n",
    )
    assert "src/app.py" in made
    target = tmp_path / "src" / "app.py"
    assert target.read_text(encoding="utf-8") == "def greet():\n    return '안녕'\n"

    tools.edit_code_file(tmp_path, "src/app.py", "greet", "welcome")
    tools.multi_edit_code_file(
        tmp_path,
        "src/app.py",
        [
            {"old_string": "welcome", "new_string": "hello"},
            {"old_string": "안녕", "new_string": "반갑습니다"},
        ],
    )
    assert target.read_text(encoding="utf-8") == "def hello():\n    return '반갑습니다'\n"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"print('ok')\x00hidden", "바이너리"),
        (b"a" * 5000 + b"\x00hidden", "바이너리"),
        (b"print('\xff')", "UTF-8"),
    ],
)
def test_code_tools_refuse_binary_or_non_utf8_overwrite(tmp_path, payload, message):
    target = tmp_path / "unsafe.py"
    target.write_bytes(payload)

    with pytest.raises(ToolError, match=message):
        tools.write_code_file(tmp_path, "unsafe.py", "print('replacement')\n")

    assert target.read_bytes() == payload


def test_code_tools_block_git_control_files_and_path_traversal(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    with pytest.raises(ToolError, match="저장소 제어"):
        tools.write_code_file(tmp_path, ".git/config", "[core]\n")
    assert not (git_dir / "config").exists()

    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    assert not outside.exists()
    with pytest.raises(ToolError, match="작업 폴더 밖"):
        tools.write_code_file(tmp_path, f"../{outside.name}", "print('escape')\n")
    assert not outside.exists()


@pytest.mark.parametrize("path", [".env", ".env.local", "certs/private.key"])
def test_code_tools_block_secret_and_certificate_paths(tmp_path, path):
    with pytest.raises(ToolError, match="비밀값|인증서"):
        tools.write_code_file(tmp_path, path, "SECRET=canary\n")
    assert not (tmp_path / path).exists()


def test_multi_edit_code_file_is_all_or_nothing(tmp_path):
    target = tmp_path / "app.ts"
    original = "const one = 1;\nconst two = 2;\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ToolError, match="2번째 편집"):
        tools.multi_edit_code_file(
            tmp_path,
            "app.ts",
            [
                {"old_string": "one = 1", "new_string": "one = 10"},
                {"old_string": "missing", "new_string": "never"},
            ],
        )

    assert target.read_text(encoding="utf-8") == original


def test_multi_edit_code_file_rejects_non_boolean_replace_all_without_writing(tmp_path):
    target = tmp_path / "app.ts"
    original = "const value = 1;\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ToolError, match="replace_all은 boolean"):
        tools.multi_edit_code_file(
            tmp_path,
            "app.ts",
            [{"old_string": "value", "new_string": "changed", "replace_all": "false"}],
        )

    assert target.read_text(encoding="utf-8") == original


def test_code_edits_match_lf_model_snippets_without_changing_crlf_file_style(tmp_path):
    target = tmp_path / "app.py"
    target.write_bytes(b"def greet():\r\n    return 'old'\r\n\r\nprint(greet())\r\n")

    tools.edit_code_file(
        tmp_path,
        "app.py",
        "def greet():\n    return 'old'",
        "def greet():\n    return 'new'",
    )
    tools.multi_edit_code_file(
        tmp_path,
        "app.py",
        [{
            "old_string": "return 'new'\n\nprint(greet())",
            "new_string": "return 'done'\n\nprint(greet())",
        }],
    )

    assert target.read_bytes() == b"def greet():\r\n    return 'done'\r\n\r\nprint(greet())\r\n"


def test_run_tool_dispatches_code_authoring_tools(tmp_path):
    tools.run_tool(
        tmp_path,
        "write_code_file",
        {"path": "package.json", "content": '{"private": true}\n'},
    )
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == '{"private": true}\n'
