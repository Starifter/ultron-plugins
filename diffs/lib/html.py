"""The diff as one self-contained HTML document.

It is drawn where no script runs: the web UI puts a view in `<iframe sandbox>`
under a policy that refuses every fetch, and the PNG and PDF are the same page
in a browser that has had its network taken away. So the page carries its own
styles, loads nothing - no web font, no image, no stylesheet - and everything a
person can do on it is what HTML and CSS do alone: `<details>` folds an
unchanged run or a whole file, and radio inputs with `:checked` switch between
unified and split and between light and dark.

**Everything the model wrote is escaped here, character by character** - the
code, the paths, the title, the section headings. Control characters become
their visible pictures and bidirectional overrides become their code points, so
a diff cannot hide what it changes behind text that reads one way and runs
another.

`static=True` is the rendering a file is made from: one layout, one theme, no
inputs and nothing folded behind a click nobody can make on a PNG.
"""

from __future__ import annotations

import difflib
import html
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import pairwise

from .engine import DiffSet, FileDiff, Line, split_lines
from .highlight import Segment, highlight_lines, lexer_for

Key = tuple[int, int]
"""A line's place in its file: which hunk, which line of it."""

DUAL_LAYOUT_ROWS = 20_000
"""Past this many rows only the chosen layout is written. Both layouts are the
whole diff twice, and a page that big is one a person scrolls rather than
toggles."""

MIN_FOLD = 4
"""An unchanged run is folded only when at least this many lines would hide.
Folding two lines behind a click costs more than it saves."""

MAX_INTRALINE_CHARS = 500
FONT_FALLBACK = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace'
FONT_NAME = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")
BIDI = re.compile("[؜‎‏‪-‮⁦-⁩]")
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """How to draw. Every field has been checked by the time it arrives."""

    title: str = ""
    theme: str = "dark"
    layout: str = "unified"
    font_family: str = "Fira Code"
    font_size: int = 15
    line_spacing: float = 1.6
    show_line_numbers: bool = True
    diff_indicators: str = "bars"
    word_wrap: bool = True
    background: bool = True
    expand_unchanged: bool = False
    lang: str = ""
    context: int = 3
    static: bool = False


# -- escaping ----------------------------------------------------------------


def escape(text: str) -> str:
    """Text as HTML that says exactly what it is and nothing more."""
    out = html.escape(text, quote=True)
    out = CONTROL.sub(lambda m: _picture(m.group(0)), out)
    return BIDI.sub(lambda m: f'<span class="cc">U+{ord(m.group(0)):04X}</span>', out)


def _picture(char: str) -> str:
    code = ord(char)
    shown = "␡" if code == 0x7F else chr(0x2400 + code)
    return f'<span class="cc">{shown}</span>'


def font_stack(family: str) -> str:
    """A CSS font stack from a setting: names that are plainly names, quoted,
    ending in `monospace`. No font is loaded - a name is used if installed."""
    names = [name.strip() for name in family.split(",")]
    quoted = [f'"{name}"' for name in names if name and FONT_NAME.match(name)]
    return ", ".join([*quoted, FONT_FALLBACK])


# -- the rows ----------------------------------------------------------------


@dataclass(slots=True)
class Row:
    key: Key
    line: Line


@dataclass(slots=True)
class Gap:
    """Unchanged lines not shown inline: the lines, when they are known, and a
    count always. `section` is a hunk's `@@` heading, from a patch."""

    count: int
    rows: list[Row] | None = None
    section: str = ""


Block = Row | Gap


def blocks_for(diff: FileDiff, context: int, expand: bool) -> list[Block]:
    """The file as rows and gaps, in order."""
    if diff.old_text is not None or diff.new_text is not None:
        return _fold_whole(diff, context, expand)
    blocks: list[Block] = []
    previous_end = 1
    for h_index, hunk in enumerate(diff.hunks):
        hidden = max(0, hunk.old_start - previous_end) if hunk.old_start > 0 else 0
        if hidden or hunk.section or h_index:
            blocks.append(Gap(hidden, None, hunk.section))
        blocks.extend(Row((h_index, i), line) for i, line in enumerate(hunk.lines))
        previous_end = hunk.old_start + hunk.old_count
    return blocks


def _fold_whole(diff: FileDiff, context: int, expand: bool) -> list[Block]:
    rows = [
        Row((h, i), line) for h, hunk in enumerate(diff.hunks) for i, line in enumerate(hunk.lines)
    ]
    if expand:
        return list(rows)
    blocks: list[Block] = []
    index = 0
    while index < len(rows):
        if rows[index].line.kind != "context":
            blocks.append(rows[index])
            index += 1
            continue
        end = index
        while end < len(rows) and rows[end].line.kind == "context":
            end += 1
        run = rows[index:end]
        keep_head = 0 if index == 0 else context
        keep_tail = 0 if end == len(rows) else context
        if len(run) - keep_head - keep_tail >= MIN_FOLD:
            blocks.extend(run[:keep_head])
            hidden = run[keep_head : len(run) - keep_tail]
            blocks.append(Gap(len(hidden), hidden))
            blocks.extend(run[len(run) - keep_tail :])
        else:
            blocks.extend(run)
        index = end
    return blocks


# -- highlighting and intraline ----------------------------------------------


def segments_for(diff: FileDiff, lang: str) -> dict[Key, list[Segment]]:
    """Every line's highlighted segments, where a lexer answered."""
    lexer = lexer_for(lang, diff.path)
    if lexer is None:
        return {}
    found: dict[Key, list[Segment]] = {}
    if diff.old_text is not None or diff.new_text is not None:
        old = highlight_lines(split_lines(diff.old_text or ""), lexer)
        new = highlight_lines(split_lines(diff.new_text or ""), lexer)
        for h, hunk in enumerate(diff.hunks):
            for i, line in enumerate(hunk.lines):
                if line.new_no is not None and new is not None:
                    found[(h, i)] = new[line.new_no - 1]
                elif line.old_no is not None and old is not None:
                    found[(h, i)] = old[line.old_no - 1]
        return found
    for h, hunk in enumerate(diff.hunks):
        for side in (("context", "del"), ("context", "add")):
            keys = [(h, i) for i, line in enumerate(hunk.lines) if line.kind in side]
            texts = [hunk.lines[i].text for _, i in keys]
            lines = highlight_lines(texts, lexer)
            if lines is None:
                continue
            for key, segments in zip(keys, lines, strict=True):
                found.setdefault(key, segments)
    return found


WORD = re.compile(r"\w+|\s+|[^\w\s]")


def intraline(diff: FileDiff) -> dict[Key, list[tuple[int, int]]]:
    """Which characters of a changed line changed, for lines paired as a
    deletion and the addition that replaced it. Lines too different to pair
    usefully are left whole."""
    marks: dict[Key, list[tuple[int, int]]] = {}
    for h, hunk in enumerate(diff.hunks):
        for dels, adds in _change_runs([Row((h, i), line) for i, line in enumerate(hunk.lines)]):
            for old, new in zip(dels, adds, strict=False):
                ranges = _word_ranges(old.line.text, new.line.text)
                if ranges is not None:
                    marks[old.key], marks[new.key] = ranges
    return marks


def _change_runs(rows: list[Row]) -> Iterator[tuple[list[Row], list[Row]]]:
    """Each run of deletions with the run of additions that follows it."""
    index = 0
    while index < len(rows):
        if rows[index].line.kind == "context":
            index += 1
            continue
        dels: list[Row] = []
        adds: list[Row] = []
        while index < len(rows) and rows[index].line.kind == "del":
            dels.append(rows[index])
            index += 1
        while index < len(rows) and rows[index].line.kind == "add":
            adds.append(rows[index])
            index += 1
        yield dels, adds


def _word_ranges(old: str, new: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    if len(old) > MAX_INTRALINE_CHARS or len(new) > MAX_INTRALINE_CHARS or old == new:
        return None
    a = WORD.findall(old)
    b = WORD.findall(new)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.ratio() < 0.4:
        return None
    a_at = _offsets(a)
    b_at = _offsets(b)
    left: list[tuple[int, int]] = []
    right: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            left.append((a_at[i1], a_at[i2]))
        if j2 > j1:
            right.append((b_at[j1], b_at[j2]))
    return _merged(left, old), _merged(right, new)


def _merged(ranges: list[tuple[int, int]], text: str) -> list[tuple[int, int]]:
    """Ranges joined across a gap of a couple of characters or of whitespace -
    a changed expression marked as one run, not as the pieces of it that
    happened to match something on the other side."""
    out: list[tuple[int, int]] = []
    for start, end in ranges:
        if out and (start - out[-1][1] <= 2 or not text[out[-1][1] : start].strip()):
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _offsets(tokens: list[str]) -> list[int]:
    out = [0]
    for token in tokens:
        out.append(out[-1] + len(token))
    return out


def code_html(text: str, segments: list[Segment] | None, marks: list[tuple[int, int]]) -> str:
    """One line's code: highlighted spans, cut again wherever a changed-word
    range starts or stops, every piece escaped."""
    if segments is None:
        segments = [("", text)] if text else []
    out: list[str] = []
    position = 0
    for cls, piece in segments:
        start, end = position, position + len(piece)
        cuts = sorted({start, end} | {p for r in marks for p in r if start < p < end})
        for a, b in pairwise(cuts):
            part = piece[a - start : b - start]
            marked = any(r0 <= a and b <= r1 for r0, r1 in marks)
            classes = " ".join(c for c in (cls, "x" if marked else "") if c)
            body = escape(part)
            out.append(f'<span class="{classes}">{body}</span>' if classes else body)
        position = end
    return "".join(out)


# -- the document ------------------------------------------------------------


def render(diffs: DiffSet, options: RenderOptions) -> str:
    """The whole page."""
    files = diffs.files
    rendered = [_FileParts(diff, options) for diff in files]
    total_rows = sum(part.row_count for part in rendered)
    both = not options.static and total_rows <= DUAL_LAYOUT_ROWS
    layouts = ("unified", "split") if both else (options.layout,)

    body: list[str] = []
    if not options.static:
        body.append(_radio("layout", "l-unified", options.layout == "unified"))
        body.append(_radio("layout", "l-split", options.layout == "split"))
        body.append(_radio("theme", "t-dark", options.theme == "dark"))
        body.append(_radio("theme", "t-light", options.theme == "light"))

    classes = ["page", f"theme-{options.theme}", f"ind-{options.diff_indicators}"]
    if options.background:
        classes.append("bg")
    if options.word_wrap or options.static:
        classes.append("wrap")
    if options.show_line_numbers:
        classes.append("nums")
    body.append(f'<div class="{" ".join(classes)}">')
    body.append(_header(diffs, options, both))
    if len(files) > 1:
        body.append(_summary(files, options.static))
    for index, part in enumerate(rendered, start=1):
        body.append(part.html(index, layouts, options))
    body.append("</div>")

    title = options.title or _default_title(diffs)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{escape(title)}</title>"
        f"<style>{stylesheet(options)}</style></head>"
        f"<body>{''.join(body)}</body></html>\n"
    )


def _default_title(diffs: DiffSet) -> str:
    if len(diffs.files) == 1:
        return diffs.files[0].path
    return f"{len(diffs.files)} files changed"


def _radio(group: str, ident: str, checked: bool) -> str:
    state = " checked" if checked else ""
    return f'<input type="radio" class="tg" name="{group}" id="{ident}"{state}>'


def _counts(additions: int, deletions: int) -> str:
    return f'<span class="ca">+{additions}</span> <span class="cd">-{deletions}</span>'


def _badge(diff: FileDiff) -> str:
    badges = []
    if diff.status != "modified":
        badges.append(f'<span class="badge b-{diff.status}">{diff.status}</span>')
    if diff.binary:
        badges.append('<span class="badge">binary</span>')
    if diff.mode_change:
        badges.append(f'<span class="badge">mode {escape(diff.mode_change)}</span>')
    return "".join(badges)


def _path_html(diff: FileDiff) -> str:
    if diff.status == "renamed" and diff.old_path != diff.new_path:
        return f'{escape(diff.old_path)} <span class="arrow">&rarr;</span> {escape(diff.new_path)}'
    return escape(diff.path)


def _header(diffs: DiffSet, options: RenderOptions, both: bool) -> str:
    title = options.title or _default_title(diffs)
    count = len(diffs.files)
    noun = "file" if count == 1 else "files"
    if diffs.changed:
        totals = f"{_counts(diffs.additions, diffs.deletions)} <span>in {count} {noun}</span>"
    else:
        totals = "<span>No changes</span>"
    toggles = ""
    if not options.static:
        layout = ""
        if both:
            layout = (
                '<span class="seg"><label for="l-unified">Unified</label>'
                '<label for="l-split">Split</label></span>'
            )
        toggles = (
            f'<div class="toggles">{layout}<span class="seg">'
            '<label for="t-dark">Dark</label><label for="t-light">Light</label></span></div>'
        )
    return (
        f'<header class="top"><div><h1>{escape(title)}</h1>'
        f'<div class="totals">{totals}</div></div>{toggles}</header>'
    )


def _summary(files: list[FileDiff], static: bool) -> str:
    items = []
    for index, diff in enumerate(files, start=1):
        name = _path_html(diff)
        link = name if static else f'<a href="#f{index}">{name}</a>'
        items.append(
            f'<li><span class="sp">{link}{_badge(diff)}</span>'
            f'<span class="sc">{_counts(diff.additions, diff.deletions)}</span></li>'
        )
    return (
        f'<nav class="summary"><h2>Changed files ({len(files)})</h2><ol>{"".join(items)}</ol></nav>'
    )


class _FileParts:
    """One file, worked out once and drawn in as many layouts as are asked for."""

    def __init__(self, diff: FileDiff, options: RenderOptions) -> None:
        self.diff = diff
        expand = options.expand_unchanged and not options.static
        self.blocks = blocks_for(diff, options.context, expand)
        self.segments = segments_for(diff, options.lang)
        self.marks = intraline(diff)
        numbers = [
            n
            for hunk in diff.hunks
            for line in hunk.lines
            for n in (line.old_no, line.new_no)
            if n is not None
        ]
        self.digits = max(2, len(str(max(numbers, default=1))))
        self.row_count = sum(len(h.lines) for h in diff.hunks)

    def html(self, index: int, layouts: tuple[str, ...], options: RenderOptions) -> str:
        diff = self.diff
        head = (
            f'<span class="fp">{_path_html(diff)}{_badge(diff)}</span>'
            f'<span class="fc">{_counts(diff.additions, diff.deletions)}</span>'
        )
        parts: list[str] = []
        if diff.binary:
            parts.append('<div class="note">Binary file - contents not shown.</div>')
        elif not self.blocks:
            parts.append('<div class="note">No content changes.</div>')
        else:
            for layout in layouts:
                rows = self._unified(options) if layout == "unified" else self._split(options)
                cls = "lay-u" if layout == "unified" else "lay-s"
                if len(layouts) == 1:
                    cls += " only"
                parts.append(f'<div class="{cls}"><div class="rows">{rows}</div></div>')
        style = f'style="--ln:{self.digits + 1}ch"'
        body = f'<div class="fb" {style}>{"".join(parts)}</div>'
        if options.static:
            return (
                f'<section class="file" id="f{index}"><div class="fh">{head}</div>{body}</section>'
            )
        return (
            f'<details class="file" id="f{index}" open>'
            f'<summary class="fh">{head}</summary>{body}</details>'
        )

    # -- unified

    def _unified(self, options: RenderOptions) -> str:
        out: list[str] = []
        for block in self.blocks:
            if isinstance(block, Row):
                out.append(self._u_row(block))
            else:
                out.append(self._gap(block, options, self._u_row))
        return "".join(out)

    def _u_row(self, row: Row) -> str:
        line = row.line
        kind = _kind(line)
        old = "" if line.old_no is None else str(line.old_no)
        new = "" if line.new_no is None else str(line.new_no)
        return (
            f'<div class="r u">'
            f'<span class="n {kind}">{old}</span><span class="n {kind}">{new}</span>'
            f'<span class="i {kind}">{_indicator(line)}</span>'
            f'<span class="c {kind}">{self._code(row)}</span></div>'
        )

    # -- split

    def _split(self, options: RenderOptions) -> str:
        out: list[str] = []
        pending: list[Row] = []

        def flush() -> None:
            for dels, adds in _change_runs(pending):
                for index in range(max(len(dels), len(adds))):
                    left = dels[index] if index < len(dels) else None
                    right = adds[index] if index < len(adds) else None
                    out.append(self._s_pair(left, right))
            pending.clear()

        for block in self.blocks:
            if isinstance(block, Row) and block.line.kind != "context":
                pending.append(block)
                continue
            flush()
            if isinstance(block, Row):
                out.append(self._s_pair(block, block))
            else:
                out.append(self._gap(block, options, lambda r: self._s_pair(r, r)))
        flush()
        return "".join(out)

    def _s_pair(self, left: Row | None, right: Row | None) -> str:
        return f'<div class="r s">{self._s_side(left, "old")}{self._s_side(right, "new")}</div>'

    def _s_side(self, row: Row | None, side: str) -> str:
        if row is None:
            edge = " l" if side == "old" else ""
            return (
                f'<span class="n e"></span><span class="i e"></span><span class="c e{edge}"></span>'
            )
        line = row.line
        kind = _kind(line)
        number = line.old_no if side == "old" else line.new_no
        shown = "" if number is None else str(number)
        return (
            f'<span class="n {kind}">{shown}</span>'
            f'<span class="i {kind}">{_indicator(line)}</span>'
            f'<span class="c {kind}{" l" if side == "old" else ""}">{self._code(row)}</span>'
        )

    # -- shared

    def _code(self, row: Row) -> str:
        return code_html(row.line.text, self.segments.get(row.key), self.marks.get(row.key, []))

    def _gap(self, gap: Gap, options: RenderOptions, draw: Callable[[Row], str]) -> str:
        noun = "line" if gap.count == 1 else "lines"
        label = f"{gap.count} unmodified {noun}" if gap.count else ""
        section = f'<span class="sec">{escape(gap.section)}</span>' if gap.section else ""
        text = (
            f"<span>&#8943; {label}</span>{section}" if label else f"<span>&#8943;</span>{section}"
        )
        if gap.rows and not options.static:
            inner = "".join(draw(row) for row in gap.rows)
            return f'<details class="fold"><summary class="gap">{text}</summary>{inner}</details>'
        return f'<div class="gap">{text}</div>'


def _kind(line: Line) -> str:
    return {"context": "ctx", "add": "add", "del": "del"}[line.kind]


def _indicator(line: Line) -> str:
    return {"context": " ", "add": "+", "del": "-"}[line.kind]


# -- styles ------------------------------------------------------------------

DARK = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "border": "#30363d",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "accent": "#58a6ff",
    "add-bg": "rgba(46,160,67,0.15)",
    "add-hl": "rgba(46,160,67,0.40)",
    "add-fg": "#3fb950",
    "del-bg": "rgba(248,81,73,0.15)",
    "del-hl": "rgba(248,81,73,0.40)",
    "del-fg": "#f85149",
    "gap-bg": "rgba(56,139,253,0.10)",
    "empty": "rgba(110,118,129,0.08)",
    "k": "#ff7b72",
    "kc": "#79c0ff",
    "kt": "#ffa657",
    "nb": "#ffa657",
    "nf": "#d2a8ff",
    "nc": "#ffa657",
    "nd": "#d2a8ff",
    "nt": "#7ee787",
    "na": "#79c0ff",
    "nn": "#ffa657",
    "s": "#a5d6ff",
    "se": "#79c0ff",
    "m": "#79c0ff",
    "c": "#8b949e",
    "cp": "#ff7b72",
    "o": "#ff7b72",
}
LIGHT = {
    "bg": "#ffffff",
    "panel": "#f6f8fa",
    "border": "#d0d7de",
    "text": "#1f2328",
    "muted": "#656d76",
    "accent": "#0969da",
    "add-bg": "rgba(26,127,55,0.12)",
    "add-hl": "rgba(26,127,55,0.30)",
    "add-fg": "#1a7f37",
    "del-bg": "rgba(207,34,46,0.10)",
    "del-hl": "rgba(207,34,46,0.28)",
    "del-fg": "#cf222e",
    "gap-bg": "rgba(84,174,255,0.12)",
    "empty": "rgba(175,184,193,0.15)",
    "k": "#cf222e",
    "kc": "#0550ae",
    "kt": "#953800",
    "nb": "#953800",
    "nf": "#8250df",
    "nc": "#953800",
    "nd": "#8250df",
    "nt": "#116329",
    "na": "#0550ae",
    "nn": "#953800",
    "s": "#0a3069",
    "se": "#0550ae",
    "m": "#0550ae",
    "c": "#6e7781",
    "cp": "#cf222e",
    "o": "#cf222e",
}
TOKENS = ("k", "kc", "kt", "nb", "nf", "nc", "nd", "nt", "na", "nn", "s", "se", "m", "c", "cp", "o")


def _vars(palette: dict[str, str]) -> str:
    return ";".join(f"--{name}:{value}" for name, value in palette.items())


def stylesheet(options: RenderOptions) -> str:
    """The page's CSS. Built from the options that are not in-page toggles."""
    ln = "var(--ln) " if options.show_line_numbers else ""
    ind = "2ch " if options.diff_indicators == "classic" else ""
    u_cols = f"{ln}{ln}{ind}minmax(0,1fr)"
    s_cols = f"{ln}{ind}minmax(0,1fr) {ln}{ind}minmax(0,1fr)"
    hide = []
    if not options.show_line_numbers:
        hide.append(".n")
    if options.diff_indicators != "classic":
        hide.append(".i")
    tokens = "".join(f".c .{t}{{color:var(--{t})}}" for t in TOKENS)
    return (
        f".page.theme-dark,#t-dark:checked~.page{{{_vars(DARK)}}}"
        f".page.theme-light,#t-light:checked~.page{{{_vars(LIGHT)}}}"
        "*{box-sizing:border-box}"
        "html,body{margin:0;padding:0}"
        ".tg{position:absolute;opacity:0;pointer-events:none;width:0;height:0}"
        ".page{background:var(--bg);color:var(--text);min-height:100vh;padding:20px;"
        "font:14px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
        ".top{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;"
        "justify-content:space-between;margin-bottom:16px}"
        "h1{font-size:20px;margin:0 0 4px;word-break:break-word}"
        ".totals{color:var(--muted)}"
        ".ca{color:var(--add-fg);font-weight:600}.cd{color:var(--del-fg);font-weight:600}"
        ".toggles{display:flex;gap:8px}"
        ".seg{display:inline-flex;border:1px solid var(--border);border-radius:6px;"
        "overflow:hidden}"
        ".seg label{padding:4px 10px;cursor:pointer;color:var(--muted);background:var(--panel)}"
        ".seg label+label{border-left:1px solid var(--border)}"
        "#l-unified:checked~.page label[for=l-unified],#l-split:checked~.page label[for=l-split],"
        "#t-dark:checked~.page label[for=t-dark],#t-light:checked~.page label[for=t-light]"
        "{color:var(--text);background:var(--bg);font-weight:600}"
        ".summary{border:1px solid var(--border);border-radius:8px;background:var(--panel);"
        "padding:10px 14px;margin-bottom:16px}"
        ".summary h2{font-size:14px;margin:0 0 6px}"
        ".summary ol{margin:0;padding-left:22px}"
        ".summary li{display:flex;justify-content:space-between;gap:12px;padding:2px 0}"
        ".summary li::marker{color:var(--muted)}"
        ".summary a{color:var(--accent);text-decoration:none}"
        ".sp{word-break:break-all}.sc{white-space:nowrap}"
        ".badge{display:inline-block;margin-left:8px;padding:0 6px;border-radius:10px;"
        "font-size:11px;border:1px solid var(--border);color:var(--muted);vertical-align:1px}"
        ".b-added{color:var(--add-fg);border-color:var(--add-fg)}"
        ".b-deleted{color:var(--del-fg);border-color:var(--del-fg)}"
        ".b-renamed{color:var(--accent);border-color:var(--accent)}"
        ".arrow{color:var(--muted)}"
        ".file{border:1px solid var(--border);border-radius:8px;margin-bottom:16px;"
        "overflow:hidden;background:var(--bg)}"
        ".fh{display:flex;justify-content:space-between;gap:12px;padding:8px 14px;"
        "background:var(--panel);border-bottom:1px solid var(--border);"
        "font-family:var(--mono);font-size:13px;cursor:pointer}"
        "summary.fh{list-style:none}summary.fh::-webkit-details-marker{display:none}"
        ".fp{word-break:break-all}.fc{white-space:nowrap}"
        ".note{padding:10px 14px;color:var(--muted)}"
        f".page{{--mono:{font_stack(options.font_family)}}}"
        f".fb{{font-family:var(--mono);font-size:{options.font_size}px;"
        f"line-height:{options.line_spacing};overflow-x:auto;tab-size:4}}"
        ".rows{width:max-content;min-width:100%}"
        ".wrap .rows,.lay-s .rows{width:auto}"
        f".r.u{{display:grid;grid-template-columns:{u_cols}}}"
        f".r.s{{display:grid;grid-template-columns:{s_cols}}}"
        ".n{text-align:right;padding:0 1ch 0 .5ch;color:var(--muted);user-select:none}"
        ".i{text-align:center;color:var(--muted);user-select:none}"
        f".c{{white-space:pre;padding:0 12px 0 10px;min-height:{options.line_spacing}em}}"
        ".wrap .c,.lay-s .c{white-space:pre-wrap;overflow-wrap:anywhere}"
        ".r.s .c.l{border-right:1px solid var(--border)}"
        ".bg .add{background:var(--add-bg)}.bg .del{background:var(--del-bg)}"
        ".n.add,.i.add{color:var(--add-fg)}.n.del,.i.del{color:var(--del-fg)}"
        ".c.add .x{background:var(--add-hl);border-radius:2px}"
        ".c.del .x{background:var(--del-hl);border-radius:2px}"
        ".ind-bars .c.add{box-shadow:inset 3px 0 0 var(--add-fg)}"
        ".ind-bars .c.del{box-shadow:inset 3px 0 0 var(--del-fg)}"
        ".e{background:var(--empty)}"
        ".gap{display:flex;gap:16px;padding:2px 14px;background:var(--gap-bg);"
        "color:var(--muted);font-size:.9em;list-style:none}"
        "summary.gap{cursor:pointer}summary.gap::-webkit-details-marker{display:none}"
        ".fold[open]>summary.gap{border-bottom:1px dashed var(--border)}"
        ".sec{color:var(--text);opacity:.75}"
        ".cc{color:var(--del-fg);border:1px solid var(--del-fg);border-radius:3px;"
        "font-size:.8em;padding:0 2px}"
        + tokens
        + (f"{','.join(hide)}{{display:none}}" if hide else "")
        + ".lay-s{display:none}.lay-s.only{display:block}"
        "#l-split:checked~.page .lay-s{display:block}"
        "#l-split:checked~.page .lay-u:not(.only){display:none}"
    )
