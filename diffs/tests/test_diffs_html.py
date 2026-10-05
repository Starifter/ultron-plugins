"""The page: escaped, self-contained, folded where it should be, in both layouts."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ultron_plugin_diffs_lib.engine import from_texts, parse_patch
from ultron_plugin_diffs_lib.highlight import highlight_lines, lexer_for, normalise_lang
from ultron_plugin_diffs_lib.html import (
    DUAL_LAYOUT_ROWS,
    RenderOptions,
    code_html,
    escape,
    font_stack,
    render,
)

HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)> https://evil.example/a.js'


ALLOWED_TAGS = {
    "html", "head", "meta", "title", "style", "body", "div", "span", "input", "label",
    "header", "h1", "h2", "nav", "ol", "li", "a", "details", "summary", "section",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {
    "lang", "charset", "name", "content", "class", "id", "type", "checked", "for",
    "href", "style", "open",
}  # fmt: skip


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.style = ""
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))
        self._in_style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.style += data


def _no_active_content(page: str) -> None:
    """Parsed as a browser would: only the tags and attributes the renderer
    writes, every link an anchor on the page, and CSS that fetches nothing."""
    parser = _Tags()
    parser.feed(page)
    for tag, attrs in parser.tags:
        assert tag in ALLOWED_TAGS, tag
        for name, value in attrs:
            assert name in ALLOWED_ATTRIBUTES, (tag, name)
            if name == "href":
                assert (value or "").startswith("#"), value
            if name == "style":
                assert "url(" not in (value or "") and "@" not in (value or "")
    assert "url(" not in parser.style and "@import" not in parser.style
    assert "http" not in parser.style


def test_model_text_is_escaped_everywhere() -> None:
    diffs = from_texts(f"a\n{HOSTILE}\n", f"b\n{HOSTILE}!\n", path=f"{HOSTILE}.py")
    page = render(diffs, RenderOptions(title=HOSTILE))
    _no_active_content(page)
    assert "&lt;script&gt;" in page
    assert "<title>&lt;script&gt;" in page


def test_patch_paths_and_sections_are_escaped() -> None:
    patch = (
        "diff --git a/<b>x</b> b/<b>x</b>\n"
        "--- a/<b>x</b>\n+++ b/<b>x</b>\n"
        "@@ -1 +1 @@ <script>bad()</script>\n-<i>a</i>\n+<i>b</i>\n"
    )
    page = render(parse_patch(patch), RenderOptions())
    _no_active_content(page)
    assert "<b>x</b>" not in page and "<i>" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page


def test_bidi_and_control_characters_are_made_visible() -> None:
    out = escape("a\u202eb\x00c\x1bd\te")
    assert "\u202e" not in out and "U+202E" in out
    assert "\x00" not in out and "\x1b" not in out
    assert "\t" in out  # tabs are code


def test_font_stack_refuses_css_injection() -> None:
    stack = font_stack('Fira Code, "}body{display:none}, JetBrains Mono')
    assert stack.startswith('"Fira Code", "JetBrains Mono"')
    assert "{" not in stack
    assert stack.endswith("monospace")


def test_long_unchanged_runs_fold_into_details() -> None:
    before = "".join(f"line {i}\n" for i in range(100))
    after = before.replace("line 50\n", "line fifty\n")
    page = render(from_texts(before, after, "x.txt"), RenderOptions(layout="unified"))
    assert page.count('<details class="fold">') == 4  # two runs, in two layouts
    assert "47 unmodified lines" in page
    assert "46 unmodified lines" in page
    # The folded lines are in the page, behind the fold.
    assert "line 10<" in page


def test_expand_unchanged_folds_nothing() -> None:
    before = "".join(f"line {i}\n" for i in range(100))
    after = before.replace("line 50\n", "line fifty\n")
    page = render(from_texts(before, after), RenderOptions(expand_unchanged=True))
    assert 'class="fold"' not in page
    assert "unmodified" not in page


def test_patch_gaps_are_counts_and_cannot_expand() -> None:
    patch = "--- a/x\n+++ b/x\n@@ -20,2 +20,2 @@ def f():\n-a\n+b\n c\n"
    page = render(parse_patch(patch), RenderOptions())
    assert "19 unmodified lines" in page
    assert 'class="fold"' not in page
    assert "def f():" in page


def test_both_layouts_and_toggles_in_a_view() -> None:
    page = render(from_texts("a\n", "b\n"), RenderOptions(layout="split", theme="light"))
    assert 'class="lay-u"' in page and 'class="lay-s"' in page
    assert re.search(r'id="l-split" checked', page)
    assert re.search(r'id="t-light" checked', page)
    assert 'label for="l-unified"' in page


def test_static_has_one_layout_and_no_inputs() -> None:
    before = "".join(f"line {i}\n" for i in range(100))
    after = before.replace("line 50\n", "line fifty\n")
    page = render(from_texts(before, after), RenderOptions(layout="split", static=True))
    assert "<input" not in page and "<details" not in page and "<label" not in page
    assert 'class="lay-s only"' in page and "lay-u" not in page.split("<body>")[1]
    assert "47 unmodified lines" in page
    _no_active_content(page)


def test_split_pairs_deletions_with_additions() -> None:
    page = render(from_texts("a\nb\n", "A\nb\nc\n"), RenderOptions(layout="split", static=True))
    rows = re.findall(r'<div class="r s">(.*?)</div>', page)
    assert len(rows) == 3
    assert 'class="n del">1<' in rows[0] and 'class="n add">1<' in rows[0]
    assert 'class="n e"' in rows[2]  # c has nothing on the left


def test_multi_file_summary_card_with_badges_and_anchors() -> None:
    patch = (
        "diff --git a/a b/b\nrename from a\nrename to b\n"
        "diff --git a/n b/n\nnew file mode 100644\n--- /dev/null\n+++ b/n\n@@ -0,0 +1 @@\n+x\n"
    )
    page = render(parse_patch(patch), RenderOptions())
    assert 'class="summary"' in page
    assert 'href="#f1"' in page and 'id="f2"' in page
    assert "b-renamed" in page and "b-added" in page
    assert "+1</span>" in page


def test_static_keeps_per_file_counts() -> None:
    patch = "--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-a\n+b\n+c\n"
    page = render(parse_patch(patch), RenderOptions(static=True))
    header = re.search(r'<div class="fh">(.*?)</div>', page)
    assert header is not None and "+2" in header.group(1) and "-1" in header.group(1)


def test_huge_diff_writes_one_layout() -> None:
    before = "".join(f"{i}\n" for i in range(DUAL_LAYOUT_ROWS + 10))
    after = before.replace("5\n", "five\n", 1)
    page = render(from_texts(before, after), RenderOptions(expand_unchanged=True))
    assert 'class="lay-u only"' in page and "lay-s only" not in page
    assert 'label for="l-split"' not in page


def test_options_reach_the_stylesheet() -> None:
    page = render(
        from_texts("a\n", "b\n"),
        RenderOptions(
            font_size=19,
            line_spacing=2.0,
            show_line_numbers=False,
            diff_indicators="classic",
            background=False,
            word_wrap=False,
        ),
    )
    assert "font-size:19px" in page and "line-height:2.0" in page
    assert ".n{display:none}" in page
    assert "bg" not in re.search(r'<div class="page([^"]*)"', page).group(1).split()  # type: ignore[union-attr]
    assert "ind-classic" in page


def test_multi_line_string_is_highlighted_from_its_start() -> None:
    lexer = lexer_for(normalise_lang("py"), "")
    lines = highlight_lines(['x = """one', "two", 'three"""', "y = 1"], lexer)
    assert lines is not None
    assert all(cls == "s" for cls, _ in lines[1])
    assert ("k", "") not in lines[3]


def test_lexer_from_file_name_and_never_guessed() -> None:
    assert lexer_for("", "src/app.ts") is not None
    assert lexer_for("", "README") is None
    assert lexer_for("text", "x.py") is None


def test_intraline_marks_the_changed_words() -> None:
    page = render(from_texts("value = compute(1)\n", "value = compute(2)\n"), RenderOptions())
    assert re.search(r'class="[^"]*\bx\b[^"]*">2<', page)


def test_code_html_cuts_segments_at_marks() -> None:
    out = code_html("abcdef", [("k", "abc"), ("", "def")], [(2, 4)])
    assert out == '<span class="k">ab</span><span class="k x">c</span><span class="x">d</span>ef'
