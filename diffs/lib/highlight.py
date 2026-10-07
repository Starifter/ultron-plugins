"""Syntax highlighting: Pygments tokens, mapped to this plugin's own classes.

Never Pygments' HTML formatter - its markup and its class names are not ours to
promise anything about, and the renderer escapes every character itself. What
leaves here is `(class, text)` pairs per line, the text still raw.

A side is tokenised whole and then cut into lines, so a string or a comment
that spans lines is coloured from where it starts. From a patch, "whole" is one
hunk's side, which is the most the patch says.

Nothing is guessed: a language comes from `lang`, or from the file's name, or
the text stays plain. A lexer chosen by sniffing content is a lexer that is
confidently wrong on the diffs where it matters.
"""

from __future__ import annotations

import re
from typing import Any

from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import (
    Comment,
    Keyword,
    Literal,
    Name,
    Number,
    Operator,
    String,
    _TokenType,
)
from pygments.util import ClassNotFound

Segment = tuple[str, str]
"""A class this plugin's CSS knows (or `""`) and the raw text it covers."""

LANG_ALIASES = {
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "node": "javascript",
    "jsx": "jsx",
    "ts": "typescript",
    "tsx": "tsx",
    "md": "markdown",
    "yml": "yaml",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "bash": "bash",
    "py": "python",
    "python3": "python",
    "rb": "ruby",
    "rs": "rust",
    "kt": "kotlin",
    "kts": "kotlin",
    "cs": "csharp",
    "c#": "csharp",
    "c++": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "h": "c",
    "golang": "go",
    "ps1": "powershell",
    "pwsh": "powershell",
    "ps": "powershell",
    "dockerfile": "docker",
    "tf": "terraform",
    "hcl": "terraform",
    "htm": "html",
    "xhtml": "html",
    "vue": "html",
    "svg": "xml",
    "jsonc": "json",
    "json5": "json",
    "text": "text",
    "txt": "text",
    "plain": "text",
    "plaintext": "text",
    "patch": "diff",
}
"""OpenClaw's short names and the usual file extensions, as Pygments names them."""

LANG_PATTERN = re.compile(r"^[A-Za-z0-9_+#.\-]{1,128}$")

MAX_HIGHLIGHT_CHARS = 600_000
"""The most one side may be and still be highlighted. Pygments' lexers are
regular expressions, and a pathological input can take a while in any of them;
past this the text is plain and the result says so."""

_CLASSES: tuple[tuple[_TokenType, str], ...] = (
    (Keyword.Constant, "kc"),
    (Keyword.Type, "kt"),
    (Keyword, "k"),
    (Operator.Word, "k"),
    (Name.Builtin, "nb"),
    (Name.Function, "nf"),
    (Name.Class, "nc"),
    (Name.Decorator, "nd"),
    (Name.Tag, "nt"),
    (Name.Attribute, "na"),
    (Name.Namespace, "nn"),
    (Name.Constant, "kc"),
    (String.Escape, "se"),
    (String.Doc, "c"),
    (String, "s"),
    (Number, "m"),
    (Literal, "m"),
    (Comment.Preproc, "cp"),
    (Comment, "c"),
    (Operator, "o"),
)
"""Most specific first. Anything unmapped is drawn in the text colour."""


def normalise_lang(lang: str) -> str:
    """A `lang` argument as Pygments names it: `js` is `javascript`. Lower-cased,
    otherwise untouched - whether Pygments knows it is `lexer_for`'s question."""
    wanted = lang.strip().lower()
    return LANG_ALIASES.get(wanted, wanted)


def lexer_for(lang: str, path: str) -> Any | None:
    """The lexer for a file: the language asked for, else the one its name says,
    else none. `stripnl=False` is load-bearing - Pygments' default drops leading
    blank lines, which would move every token after them onto the wrong line."""
    options: dict[str, Any] = {"stripnl": False, "stripall": False, "ensurenl": True}
    if lang:
        if lang == "text":
            return None
        try:
            return get_lexer_by_name(lang, **options)
        except ClassNotFound:
            return None
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        return None
    try:
        return get_lexer_for_filename(name, **options)
    except ClassNotFound:
        return None


def known_lang(lang: str) -> bool:
    if lang == "text":
        return True
    try:
        get_lexer_by_name(lang)
    except ClassNotFound:
        return False
    return True


def css_class(ttype: _TokenType) -> str:
    for parent, name in _CLASSES:
        if ttype in parent:
            return name
    return ""


def highlight_lines(lines: list[str], lexer: Any | None) -> list[list[Segment]] | None:
    """Each line's segments, or `None` when there is nothing to highlight with
    or the side is too large. A lexer that disagrees with us about how many
    lines there are is ignored rather than trusted."""
    if lexer is None or not lines:
        return None
    text = "\n".join(lines)
    if len(text) > MAX_HIGHLIGHT_CHARS:
        return None
    out: list[list[Segment]] = [[]]
    try:
        for ttype, value in lexer.get_tokens(text):
            cls = css_class(ttype)
            parts = value.split("\n")
            for index, part in enumerate(parts):
                if index:
                    out.append([])
                if part:
                    out[-1].append((cls, part))
    except Exception:
        return None
    # `ensurenl` adds one final newline, and so one empty trailing line.
    while len(out) > len(lines) and not out[-1]:
        out.pop()
    if len(out) != len(lines):
        return None
    for segments, original in zip(out, lines, strict=True):
        if "".join(text for _, text in segments) != original:
            return None
    return out
