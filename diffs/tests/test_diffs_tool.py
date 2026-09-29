"""The tool: what it refuses, what it writes, and what the model is told."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ultron_plugin_diffs_lib.settings import Settings
from ultron_plugin_diffs_lib.store import DiffStore
from ultron_plugin_diffs_lib.tool import MAX_PATCH_BYTES, MAX_TEXT_BYTES, DiffsTool

from ultron.sdk.tool_plugin import ToolResult

PATCH = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"


def tool(tmp_path: Path, **settings: Any) -> DiffsTool:
    parsed, notes = Settings.read(settings)
    return DiffsTool(parsed, DiffStore(tmp_path), setting_notes=notes)


def facts(result: ToolResult) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in result.content.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.replace("_", "").isalpha() and key not in out:
            out[key] = value
    return out


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"patch": PATCH, "before": "a"}, "not both"),
        ({"patch": PATCH, "after": "a"}, "not both"),
        ({"before": "a"}, "before and after"),
        ({}, "before and after"),
        ({"patch": "   "}, "empty"),
        ({"patch": "hello"}, "no file diffs"),
        ({"before": "a", "after": "b", "mode": "movie"}, "mode must be"),
        ({"before": "a", "after": "b", "theme": "blue"}, "theme must be"),
        ({"before": "a", "after": "b", "layout": "grid"}, "layout must be"),
        ({"before": "a", "after": "b", "file_format": "gif"}, "file_format must be"),
        ({"before": "a", "after": "b", "file_quality": "max"}, "file_quality must be"),
        ({"before": "a", "after": "b", "file_scale": 5}, "file_scale"),
        ({"before": "a", "after": "b", "file_scale": True}, "file_scale"),
        ({"before": "a", "after": "b", "file_max_width": 100}, "file_max_width"),
        ({"before": "a", "after": "b", "ttl_seconds": 21_601}, "ttl_seconds"),
        ({"before": "a", "after": "b", "lang": "py<script>"}, "lang"),
        ({"before": "a", "after": "b", "lang": "x" * 129}, "lang is"),
        ({"before": "a", "after": "b", "path": "p" * 2049}, "path is"),
        ({"before": "a", "after": "b", "title": "t" * 1025}, "title is"),
        ({"before": "a", "after": "b", "expand_unchanged": "yes"}, "expand_unchanged"),
        ({"before": 1, "after": "b"}, "strings"),
    ],
)
async def test_refusals_are_results(
    tmp_path: Path, arguments: dict[str, Any], message: str
) -> None:
    result = await tool(tmp_path).run(**arguments)
    assert result.is_error
    assert message in result.content
    assert not (tmp_path / ".ultron").exists()


async def test_size_caps(tmp_path: Path) -> None:
    big = "x" * (MAX_TEXT_BYTES + 1)
    result = await tool(tmp_path).run(before=big, after="")
    assert result.is_error and "before is" in result.content
    result = await tool(tmp_path).run(before="", after="é" * (MAX_TEXT_BYTES // 2 + 1))
    assert result.is_error and "after is" in result.content  # bytes, not characters
    result = await tool(tmp_path).run(patch="+" * (MAX_PATCH_BYTES + 1))
    assert result.is_error and "patch is" in result.content


async def test_view_mode_writes_the_page_and_reports(tmp_path: Path) -> None:
    result = await tool(tmp_path).run(before="a\n", after="b\n", path="x.py", mode="view")
    assert not result.is_error, result.content
    got = facts(result)
    assert got["changed"] == "true"
    assert got["input_kind"] == "before_after"
    assert got["file_count"] == "1"
    assert (got["additions"], got["deletions"]) == ("1", "1")
    assert got["title"] == "x.py"
    assert got["mode"] == "view"
    assert got["expires_at"].endswith("Z")
    assert "web UI" in result.content
    assert "file_path" not in got
    stored = DiffStore(tmp_path).read_view(got["artifact_id"])
    assert stored is not None and "x.py" in stored[0]


async def test_patch_input_and_notes(tmp_path: Path) -> None:
    result = await tool(tmp_path).run(
        patch=PATCH, mode="view", path="ignored", lang="klingon", expand_unchanged=True
    )
    got = facts(result)
    assert got["input_kind"] == "patch"
    assert "path is ignored" in result.content
    assert "'klingon' is not a language" in result.content
    assert "expand_unchanged does nothing" in result.content


async def test_bad_settings_fall_back_with_a_note(tmp_path: Path) -> None:
    result = await tool(tmp_path, theme="purple", font_size="big").run(
        before="a", after="b", mode="view"
    )
    assert not result.is_error
    assert "setting theme='purple'" in result.content
    assert "setting font_size='big'" in result.content


async def test_mode_defaults_from_settings(tmp_path: Path) -> None:
    result = await tool(tmp_path, mode="view").run(before="a", after="b")
    assert facts(result)["mode"] == "view"


async def test_file_failure_in_file_mode_is_an_error_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    result = await tool(tmp_path, executable=str(tmp_path / "no-such-browser")).run(
        before="a", after="b", mode="file"
    )
    assert result.is_error
    assert "could not render" in result.content
    assert list((tmp_path / ".ultron" / "diffs").iterdir()) == []


async def test_file_failure_in_both_mode_still_returns_the_view(tmp_path: Path) -> None:
    result = await tool(tmp_path, executable=str(tmp_path / "no-such-browser")).run(
        before="a", after="b", mode="both"
    )
    assert not result.is_error
    got = facts(result)
    assert "file_error" in got and "not a file" in got["file_error"]
    assert DiffStore(tmp_path).read_view(got["artifact_id"]) is not None


async def test_quality_sets_the_default_scale(tmp_path: Path) -> None:
    t = tool(tmp_path)
    call = t.prepare({"before": "a", "after": "b", "file_quality": "print"})
    assert call.file_scale == 4
    call = t.prepare({"before": "a", "after": "b", "file_quality": "print", "file_scale": 1})
    assert call.file_scale == 1
    assert t.prepare({"before": "a", "after": "b"}).file_scale == 2


async def test_view_reference_rides_the_result(tmp_path: Path) -> None:
    made: list[tuple[str, str]] = []

    def view(artifact_id: str, title: str) -> Any:
        made.append((artifact_id, title))
        return ("view", artifact_id)

    parsed, _ = Settings.read({})
    t = DiffsTool(parsed, DiffStore(tmp_path), view=view)
    result = await t.run(before="a", after="b", title="My diff", mode="view")
    got = facts(result)
    assert made == [(got["artifact_id"], "My diff")]
    reference: Any = result.view
    assert reference == ("view", got["artifact_id"])


def test_schema_is_what_validate_reads(tmp_path: Path) -> None:
    t = tool(tmp_path)
    assert t.validate({"before": "a", "after": "b", "file_scale": 2}) == {
        "before": "a",
        "after": "b",
        "file_scale": 2,
    }
    spec = t.spec()
    assert spec.name == "diffs"
    assert "secret" in spec.description and "pdf" in spec.description
