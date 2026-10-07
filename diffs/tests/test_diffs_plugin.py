"""The plugin against the SDK: needs 1.33 (`register_view`, `ViewDocument`)."""

from __future__ import annotations

from pathlib import Path

from ultron_plugin_diffs import DiffsPlugin, view
from ultron_plugin_diffs_lib.store import DiffStore

from ultron.sdk.plugin_entry import PluginContext


async def test_view_serves_a_live_artifact_and_nothing_else(tmp_path: Path) -> None:
    store = DiffStore(tmp_path)
    artifact = store.create({"title": "T"}, 60, "<p>hi</p>")
    document = await view(artifact.id, tmp_path)
    assert document is not None
    assert document.html == "<p>hi</p>" and document.title == "T"
    assert await view("../../etc/passwd", tmp_path) is None
    assert await view("A" * 22, tmp_path) is None


def test_register_installs_the_view(tmp_path: Path) -> None:
    ctx = PluginContext("diffs", workspace=tmp_path)
    DiffsPlugin().register(ctx)
    assert ctx.views == ["diffs"]
    reference = ctx.view("abc", "T")
    assert (reference.plugin, reference.id, reference.title) == ("diffs", "abc", "T")
