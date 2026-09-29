"""The `diffs` plugin: OpenClaw's diffs tool, for Ultron.

Two provisions: the `diffs` tool, and the plugin's view, which answers an
artifact id with the page the tool stored, for as long as it lives. The core
puts its own policy in front of that page and the web UI draws it in a sandbox
with no scripts (`plugin-sdk.md` §5.4), so nothing here decides what a viewer
may run.

Ultron imports this one file under a name of its own. The rest of the plugin is
the `lib/` package beside it, loaded here from this directory and never from
`sys.path` - so no other code on the machine can stand in for it, and nothing
this plugin does changes what anybody else imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.tool_plugin import ViewDocument

LIB = "ultron_plugin_diffs_lib"
"""The name `lib/` is loaded under: this plugin's own, like the one Ultron gives
this file, so it cannot meet a package of the same name."""


def _load_lib() -> ModuleType:
    """`lib/` from beside this file, fresh.

    Fresh, because an update replaces the directory and the next session
    re-imports this file: a submodule left in `sys.modules` from before would
    answer the new code's relative imports with the old code."""
    for name in [n for n in sys.modules if n == LIB or n.startswith(LIB + ".")]:
        del sys.modules[name]
    here = Path(__file__).resolve().parent / "lib"
    spec = importlib.util.spec_from_file_location(
        LIB, here / "__init__.py", submodule_search_locations=[str(here)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - a broken install
        raise ImportError(f"diffs: cannot load {here}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[LIB] = module
    spec.loader.exec_module(module)
    return module


_load_lib()

from ultron_plugin_diffs_lib.settings import Settings  # noqa: E402
from ultron_plugin_diffs_lib.store import DiffStore  # noqa: E402
from ultron_plugin_diffs_lib.tool import DiffsTool  # noqa: E402


async def view(artifact_id: str, workspace: Path) -> ViewDocument | None:
    """The stored page for an id, or `None` - malformed, unknown and expired
    are all the same answer, *not found*."""
    found = DiffStore(Path(workspace)).read_view(str(artifact_id))
    if found is None:
        return None
    page, meta = found
    title = meta.get("title")
    return ViewDocument(html=page, title=title if isinstance(title, str) else "")


class DiffsPlugin(Plugin):
    name = "diffs"
    description = "Diffs rendered for a person: a viewer in the web UI, a PNG or a PDF."

    def register(self, ctx: PluginContext) -> None:
        settings, notes = Settings.read(ctx.settings)
        ctx.register_view(view)
        if ctx.accepts_tools:
            ctx.register_tool(
                DiffsTool(
                    settings,
                    DiffStore(Path(ctx.workspace)),
                    view=ctx.view,
                    audit=ctx.audit,
                    setting_notes=notes,
                )
            )
