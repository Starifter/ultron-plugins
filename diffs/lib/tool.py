"""The `diffs` tool: a diff made for a person to read, not for the model.

The model already has the change - it wrote it, or read it. What this tool adds
is a rendering: a viewer the web UI opens from the call's card, a PNG or a PDF
the model can send to a channel, or both. The result the model reads is a few
facts about what was made, never the page.

The work is in three steps, and only the last one touches the disk: the diff
is built and drawn in a thread (`difflib` and Pygments on half a megabyte are
not free), then `assert_active()`, then the artifact is written and, for a
file, a browser is launched to photograph it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ultron.sdk.runtime import assert_active
from ultron.sdk.tool_plugin import Tool, ToolResult

from .engine import DiffError, DiffSet, from_texts, parse_patch
from .files import QUALITY_PRESETS, Rendered, RenderError, render_file
from .highlight import LANG_PATTERN, known_lang, normalise_lang
from .html import RenderOptions, render
from .settings import (
    FILE_FORMATS,
    FILE_QUALITIES,
    INDICATORS,
    LAYOUTS,
    MAX_WIDTH_RANGE,
    MODES,
    SCALE_RANGE,
    THEMES,
    TTL_RANGE,
    Settings,
)
from .store import DiffStore

MAX_TEXT_BYTES = 512 * 1024
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_PATH_BYTES = 2048
MAX_LANG_BYTES = 128
MAX_TITLE_BYTES = 1024

ViewFactory = Callable[[str, str], Any]
"""`PluginContext.view`: an artifact id and a title to the `View` a result carries."""
Audit = Callable[..., None]
"""`PluginContext.audit`."""

DESCRIPTION = """\
Render a diff for a person to read: a viewer they open from this call's card in \
the web UI, a PNG or PDF file, or both. You get back facts about what was made, \
not the diff itself.
- Give before and after (the whole old and new text of one file, with path for \
its name and highlighting), or patch (a unified diff, one file or many, e.g. git \
diff output) - never both.
- mode=view (default both) is for the person at the web UI. Use mode=file when the \
diff must travel - send file_path to a channel with the message tool; \
file_format=pdf for channels that recompress images, since it stays sharp and \
pages a long diff. The file is kept until expires_at.
- Diffs are stored in the workspace and shown to whoever opens them: do not put \
secrets, keys or tokens in before, after or patch.
- Only before/after can expand unchanged lines (expand_unchanged); a patch only \
has the lines its hunks carry."""


@dataclass(frozen=True, slots=True)
class Call:
    """One call's arguments, checked, with the session's settings filled in."""

    diffs: DiffSet
    mode: str
    title: str
    options: RenderOptions
    file_format: str
    file_quality: str
    file_scale: int
    file_max_width: int
    ttl_seconds: int
    notes: tuple[str, ...]


class DiffsTool(Tool):
    name = "diffs"
    description = DESCRIPTION
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "before": {"type": "string", "description": "The old text of one file."},
            "after": {"type": "string", "description": "The new text of the same file."},
            "patch": {
                "type": "string",
                "description": "A unified diff instead of before/after; may cover many files.",
            },
            "path": {
                "type": "string",
                "description": "The file's name for before/after - shown, and picks highlighting.",
            },
            "lang": {
                "type": "string",
                "description": "Language for highlighting (python, ts, md, yml, bash...); "
                "default from the file name.",
            },
            "title": {"type": "string", "description": "A heading for the diff."},
            "mode": {"type": "string", "enum": list(MODES)},
            "theme": {"type": "string", "enum": list(THEMES)},
            "layout": {"type": "string", "enum": list(LAYOUTS)},
            "expand_unchanged": {
                "type": "boolean",
                "description": "Show every unchanged line instead of folding them (before/after).",
            },
            "file_format": {"type": "string", "enum": list(FILE_FORMATS)},
            "file_quality": {"type": "string", "enum": list(FILE_QUALITIES)},
            "file_scale": {"type": "integer", "minimum": 1, "maximum": 4},
            "file_max_width": {"type": "integer", "minimum": 640, "maximum": 2400},
            "ttl_seconds": {
                "type": "integer",
                "description": "How long the viewer and file are kept; default 1800, most 21600.",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        settings: Settings,
        store: DiffStore,
        *,
        view: ViewFactory | None = None,
        audit: Audit | None = None,
        setting_notes: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._view = view
        self._audit = audit
        self._setting_notes = tuple(setting_notes or ())

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            call = await asyncio.to_thread(self.prepare, arguments)
        except (ArgumentError, DiffError) as error:
            return ToolResult.error(str(error))
        return await self.execute(call)

    # -- checking ------------------------------------------------------------

    def prepare(self, a: Mapping[str, Any]) -> Call:
        """Every argument checked, the diff built. Raises `ArgumentError`."""
        s = self.settings
        notes: list[str] = list(self._setting_notes)
        before, after, patch = a.get("before"), a.get("after"), a.get("patch")
        if patch is not None and (before is not None or after is not None):
            raise ArgumentError("give patch, or before and after - not both")
        path = _text(a, "path", MAX_PATH_BYTES)
        lang_raw = _text(a, "lang", MAX_LANG_BYTES)
        title = _text(a, "title", MAX_TITLE_BYTES)
        lang = ""
        if lang_raw:
            if not LANG_PATTERN.match(lang_raw):
                raise ArgumentError("lang is a language name, like python or ts")
            lang = normalise_lang(lang_raw)
            if not known_lang(lang):
                notes.append(f"lang {lang_raw!r} is not a language this knows; shown as plain text")
                lang = "text"

        if patch is not None:
            if not isinstance(patch, str):
                raise ArgumentError("patch must be a string")
            _cap("patch", patch, MAX_PATCH_BYTES)
            if not patch.strip():
                raise ArgumentError("patch is empty")
            diffs = parse_patch(patch)
            if path:
                notes.append("path is ignored with a patch; its file names come from the patch")
        else:
            if before is None or after is None:
                raise ArgumentError("give before and after (both), or patch")
            if not isinstance(before, str) or not isinstance(after, str):
                raise ArgumentError("before and after must be strings")
            _cap("before", before, MAX_TEXT_BYTES)
            _cap("after", after, MAX_TEXT_BYTES)
            diffs = from_texts(before, after, path)

        mode = _choice(a, "mode", MODES, s.mode)
        theme = _choice(a, "theme", THEMES, s.theme)
        layout = _choice(a, "layout", LAYOUTS, s.layout)
        expand = a.get("expand_unchanged", False)
        if not isinstance(expand, bool):
            raise ArgumentError("expand_unchanged must be true or false")
        if expand and diffs.input_kind == "patch":
            notes.append(
                "expand_unchanged does nothing for a patch: its unchanged lines are not in it"
            )
        file_format = _choice(a, "file_format", FILE_FORMATS, s.file_format)
        file_quality = _choice(a, "file_quality", FILE_QUALITIES, s.file_quality)
        if "file_scale" in a:
            file_scale = _integer(a, "file_scale", SCALE_RANGE)
        elif "file_quality" in a:
            file_scale = QUALITY_PRESETS[file_quality][0]
        else:
            file_scale = s.file_scale
        file_max_width = (
            _integer(a, "file_max_width", MAX_WIDTH_RANGE)
            if "file_max_width" in a
            else s.file_max_width
        )
        ttl = _integer(a, "ttl_seconds", TTL_RANGE) if "ttl_seconds" in a else s.ttl_seconds
        if mode == "view" and any(
            key in a for key in ("file_format", "file_quality", "file_scale", "file_max_width")
        ):
            notes.append("file_* arguments do nothing with mode=view")

        options = RenderOptions(
            title=title,
            theme=theme,
            layout=layout,
            font_family=s.font_family,
            font_size=s.font_size,
            line_spacing=s.line_spacing,
            show_line_numbers=s.show_line_numbers,
            diff_indicators=s.diff_indicators if s.diff_indicators in INDICATORS else "bars",
            word_wrap=s.word_wrap,
            background=s.background,
            expand_unchanged=expand,
            lang=lang,
        )
        return Call(
            diffs=diffs,
            mode=mode,
            title=title or _default_title(diffs),
            options=options,
            file_format=file_format,
            file_quality=file_quality,
            file_scale=file_scale,
            file_max_width=file_max_width,
            ttl_seconds=ttl,
            notes=tuple(notes),
        )

    # -- doing ---------------------------------------------------------------

    async def execute(self, call: Call) -> ToolResult:
        wants_view = call.mode in ("view", "both")
        wants_file = call.mode in ("file", "both")
        page = await asyncio.to_thread(render, call.diffs, call.options) if wants_view else None
        static = (
            await asyncio.to_thread(render, call.diffs, replace(call.options, static=True))
            if wants_file
            else None
        )

        assert_active()  # the last moment before anything is written
        diffs = call.diffs
        meta = {
            "title": call.title,
            "input_kind": diffs.input_kind,
            "file_count": len(diffs.files),
            "additions": diffs.additions,
            "deletions": diffs.deletions,
            "changed": diffs.changed,
            "mode": call.mode,
        }
        artifact = self.store.create(meta, call.ttl_seconds, page)
        removed = self.store.sweep(keep=artifact.id)
        if removed and self._audit is not None:
            self._audit("sweep", f"removed {len(removed)} expired diff(s)")

        lines = [
            f"changed: {'true' if diffs.changed else 'false'}",
            f"artifact_id: {artifact.id}",
            f"title: {call.title}",
            f"expires_at: {artifact.expires_at}",
            f"input_kind: {diffs.input_kind}",
            f"file_count: {len(diffs.files)}",
            f"additions: {diffs.additions}",
            f"deletions: {diffs.deletions}",
            f"mode: {call.mode}",
        ]
        file_error = ""
        if wants_file and static is not None:
            try:
                rendered = await self._render(static, call)
            except RenderError as error:
                file_error = str(error)
            else:
                assert_active()
                target = artifact.directory / f"diff.{rendered.file_format}"
                target.write_bytes(rendered.data)
                relative = target.relative_to(self.store.workspace).as_posix()
                self.store.update_meta(artifact, file=target.name, file_bytes=len(rendered.data))
                lines += [
                    f"file_path: {relative}",
                    f"file_bytes: {len(rendered.data)}",
                    f"file_format: {rendered.file_format}",
                    f"file_quality: {call.file_quality}",
                    f"file_scale: {call.file_scale}",
                    f"file_max_width: {call.file_max_width}",
                ]
                if rendered.pages:
                    lines.append(f"file_pages: {rendered.pages}")
            if file_error and not wants_view:
                self.store.remove(artifact.id)
                return ToolResult.error(f"could not render the diff as a file: {file_error}")
            if file_error:
                lines.append(f"file_error: {file_error}")
        if wants_view:
            lines.append("The diff opens from this tool call's card in the web UI.")
        lines += [f"note: {note}" for note in call.notes]
        content = "\n".join(lines)
        if wants_view and self._view is not None:
            return ToolResult(content=content, view=self._view(artifact.id, call.title))
        return ToolResult.ok(content)

    async def _render(self, page: str, call: Call) -> Rendered:
        started = time.monotonic()
        try:
            rendered = await render_file(
                page,
                file_format=call.file_format,
                scale=call.file_scale,
                max_width=call.file_max_width,
                quality=call.file_quality,
                executable=self.settings.executable,
            )
        except RenderError as error:
            self._record("render", str(error), "error", call, started)
            raise
        self._record(
            "render", f"{rendered.file_format}, {len(rendered.data)} bytes", "ok", call, started
        )
        return rendered

    def _record(self, event: str, detail: str, outcome: str, call: Call, started: float) -> None:
        if self._audit is None:
            return
        self._audit(
            event,
            detail,
            outcome=outcome,
            arguments={"file_format": call.file_format, "file_quality": call.file_quality},
            duration_ms=(time.monotonic() - started) * 1000,
        )


class ArgumentError(ValueError):
    """An argument the tool cannot use. The message is for the model."""


def _default_title(diffs: DiffSet) -> str:
    if len(diffs.files) == 1:
        return diffs.files[0].path
    return f"{len(diffs.files)} files changed"


def _text(a: Mapping[str, Any], key: str, limit: int) -> str:
    value = a.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ArgumentError(f"{key} must be a string")
    _cap(key, value, limit)
    return value.strip()


def _cap(key: str, value: str, limit: int) -> None:
    size = len(value.encode("utf-8", errors="surrogatepass"))
    if size > limit:
        raise ArgumentError(f"{key} is {size:,} bytes; the most is {limit:,}")


def _choice(a: Mapping[str, Any], key: str, options: tuple[str, ...], default: str) -> str:
    value = a.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value.strip().lower() not in options:
        raise ArgumentError(f"{key} must be one of {', '.join(options)}")
    return value.strip().lower()


def _integer(a: Mapping[str, Any], key: str, bounds: tuple[int, int]) -> int:
    value = a.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not bounds[0] <= value <= bounds[1]:
        raise ArgumentError(f"{key} must be a whole number from {bounds[0]} to {bounds[1]}")
    return value
