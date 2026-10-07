"""The diff model: what changed, as data, before anything is drawn.

Two ways in, and they end in the same shape:

- `from_texts` - a `before` and an `after`. `difflib` finds the change and the
  whole of both sides is kept, so every unchanged line can be shown when a
  person asks for it and a multi-line string is highlighted from its start.
- `parse_patch` - a unified diff, one file or many, `git diff` or plain `diff
  -u`. Only what the hunks carry is known, so the gaps between them are counts
  and never lines.

A `DiffSet` is plain data - no HTML, no Pygments - so the renderer, the tool
and the tests read the same thing and nothing here can be talked into markup.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

LineKind = Literal["context", "add", "del"]
Status = Literal["modified", "added", "deleted", "renamed"]
InputKind = Literal["before_after", "patch"]

MAX_PATCH_FILES = 128
MAX_PATCH_LINES = 120_000
"""OpenClaw's patch limits: the most files one patch may touch, and the most
lines it may be. Counted before parsing, so a patch past either costs nothing."""


class DiffError(ValueError):
    """Input that cannot be made into a diff. The message is for the model."""


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a hunk. `old_no` is absent on an addition, `new_no` on a deletion."""

    kind: LineKind
    text: str
    old_no: int | None
    new_no: int | None


@dataclass(slots=True)
class Hunk:
    """A run of lines with its place on both sides. `section` is the text after
    the second `@@`, which git fills with the enclosing function when it can."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[Line] = field(default_factory=list)


@dataclass(slots=True)
class FileDiff:
    """One file's change.

    `old_text` and `new_text` are the whole sides when they are known - from
    `before` and `after` - and `None` from a patch, which is what tells the
    renderer that a gap between hunks cannot be expanded."""

    old_path: str
    new_path: str
    status: Status = "modified"
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    mode_change: str = ""
    old_text: str | None = None
    new_text: str | None = None

    @property
    def path(self) -> str:
        """The name a person reads: the new one, unless the file is gone."""
        return self.old_path if self.status == "deleted" else self.new_path

    @property
    def additions(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind == "add")

    @property
    def deletions(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind == "del")

    @property
    def changed(self) -> bool:
        return bool(
            self.additions
            or self.deletions
            or self.status != "modified"
            or self.binary
            or self.mode_change
        )


@dataclass(slots=True)
class DiffSet:
    """Every file one call is about, and how they arrived."""

    files: list[FileDiff]
    input_kind: InputKind

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def changed(self) -> bool:
        return any(f.changed for f in self.files)


# -- lines -----------------------------------------------------------------


def split_lines(text: str) -> list[str]:
    """Lines without their endings, `\\r\\n` and lone `\\r` included.

    Not `str.splitlines`, which also breaks on form feeds, vertical tabs and
    Unicode separators - a diff that disagreed with `git` about where a line
    ends would number every line after the first one wrong."""
    if not text:
        return []
    lines = re.split(r"\r\n|\n|\r", text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


# -- before and after ------------------------------------------------------


def from_texts(before: str, after: str, path: str = "") -> DiffSet:
    """A diff of two texts, with every line of both kept.

    The file is one hunk covering both sides whole: which unchanged runs to
    fold away is the renderer's decision, made with the context it was given,
    and a hunk here that had already dropped them could not be unfolded."""
    old = split_lines(before)
    new = split_lines(after)
    name = path or "untitled"
    hunk = Hunk(
        old_start=1 if old else 0, old_count=len(old), new_start=1 if new else 0, new_count=len(new)
    )
    matcher = difflib.SequenceMatcher(None, old, new)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                hunk.lines.append(
                    Line("context", old[i1 + offset], i1 + offset + 1, j1 + offset + 1)
                )
            continue
        # Deletions before additions, as a unified diff prints them, so the
        # split view can pair the two runs row by row.
        for index in range(i1, i2):
            hunk.lines.append(Line("del", old[index], index + 1, None))
        for index in range(j1, j2):
            hunk.lines.append(Line("add", new[index], None, index + 1))
    diff = FileDiff(old_path=name, new_path=name, hunks=[hunk], old_text=before, new_text=after)
    return DiffSet(files=[diff], input_kind="before_after")


# -- unified patches -------------------------------------------------------

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
GIT_HEADER = re.compile(r"^diff --git (.+)$")
DEV_NULL = "/dev/null"


def count_patch_lines(patch: str) -> int:
    return len(split_lines(patch))


def parse_patch(patch: str) -> DiffSet:
    """Every file a unified diff describes.

    Reads `git diff` output - renames, copies, new and deleted files, mode
    changes, binary files - and plain `diff -u` output, where a file starts at
    its `---` line. A hunk is read by its counts rather than by what its lines
    look like, so a deleted line that reads `-- foo` is a deletion and not the
    next file's header."""
    lines = split_lines(patch)
    if len(lines) > MAX_PATCH_LINES:
        raise DiffError(f"patch is {len(lines):,} lines; the most is {MAX_PATCH_LINES:,}")
    files: list[FileDiff] = []
    current: FileDiff | None = None
    # `diff --git` opened a file whose `---`/`+++` have not been seen yet.
    in_git_header = False
    index = 0

    def start(old: str, new: str) -> FileDiff:
        diff = FileDiff(old_path=old, new_path=new)
        files.append(diff)
        if len(files) > MAX_PATCH_FILES:
            raise DiffError(f"patch touches more than {MAX_PATCH_FILES} files")
        return diff

    while index < len(lines):
        line = lines[index]
        git = GIT_HEADER.match(line)
        if git:
            old, new = _git_paths(git.group(1))
            current = start(old, new)
            in_git_header = True
            index += 1
            continue
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            old = _header_path(line[4:])
            new = _header_path(lines[index + 1][4:])
            if current is None or not in_git_header:
                current = start(old if old != DEV_NULL else new, new if new != DEV_NULL else old)
            if old == DEV_NULL:
                current.status = "added"
            elif new == DEV_NULL:
                current.status = "deleted"
            else:
                current.old_path, current.new_path = old, new
            in_git_header = False
            index += 2
            continue
        header = HUNK_HEADER.match(line)
        if header and current is not None:
            in_git_header = False
            hunk, index = _read_hunk(header, lines, index + 1)
            current.hunks.append(hunk)
            continue
        if current is not None and in_git_header:
            _git_extended(current, line)
        index += 1

    if not files:
        raise DiffError(
            "patch has no file diffs - expected unified diff output "
            "(`diff --git`, or `---`/`+++` lines followed by `@@` hunks)"
        )
    for diff in files:
        if diff.status == "modified" and diff.old_path != diff.new_path:
            diff.status = "renamed"
    return DiffSet(files=files, input_kind="patch")


def _read_hunk(header: re.Match[str], lines: list[str], index: int) -> tuple[Hunk, int]:
    old_start = int(header.group(1))
    old_count = int(header.group(2)) if header.group(2) is not None else 1
    new_start = int(header.group(3))
    new_count = int(header.group(4)) if header.group(4) is not None else 1
    hunk = Hunk(old_start, old_count, new_start, new_count, header.group(5).strip())
    old_no, new_no = old_start, new_start
    old_left, new_left = old_count, new_count
    while index < len(lines) and (old_left > 0 or new_left > 0):
        line = lines[index]
        marker, text = line[:1], line[1:]
        if marker == "\\":
            index += 1  # "\ No newline at end of file"
            continue
        if marker == "-" and old_left > 0:
            hunk.lines.append(Line("del", text, old_no, None))
            old_no += 1
            old_left -= 1
        elif marker == "+" and new_left > 0:
            hunk.lines.append(Line("add", text, None, new_no))
            new_no += 1
            new_left -= 1
        elif marker in (" ", "") and old_left > 0 and new_left > 0:
            # An empty line is a context line whose leading space an editor
            # or a mail client stripped.
            hunk.lines.append(Line("context", text, old_no, new_no))
            old_no += 1
            new_no += 1
            old_left -= 1
            new_left -= 1
        else:
            break  # the counts lied; stop where the hunk stops making sense
        index += 1
    while index < len(lines) and lines[index].startswith("\\"):
        index += 1
    return hunk, index


def _git_extended(diff: FileDiff, line: str) -> None:
    """The lines git writes between `diff --git` and `---`."""
    if line.startswith("new file mode"):
        diff.status = "added"
    elif line.startswith("deleted file mode"):
        diff.status = "deleted"
    elif line.startswith(("rename from ", "copy from ")):
        diff.old_path = _unquote(line.split(" from ", 1)[1])
        diff.status = "renamed"
    elif line.startswith(("rename to ", "copy to ")):
        diff.new_path = _unquote(line.split(" to ", 1)[1])
        diff.status = "renamed"
    elif line.startswith("old mode "):
        diff.mode_change = line[len("old mode ") :].strip() + diff.mode_change
    elif line.startswith("new mode "):
        diff.mode_change = f"{diff.mode_change} -> {line[len('new mode ') :].strip()}"
    elif line.startswith(("Binary files ", "GIT binary patch")):
        diff.binary = True


def _git_paths(rest: str) -> tuple[str, str]:
    """The two paths of a `diff --git` line, quoted or not.

    Unquoted paths may contain spaces, so the split is on ` b/` - the one
    place the line says where the second path begins."""
    rest = rest.strip()
    if rest.startswith('"'):
        first, _, tail = _take_quoted(rest)
        return _strip_prefix(first), _strip_prefix(_unquote(tail.strip()))
    if " b/" in rest:
        old, new = rest.split(" b/", 1)
        return _strip_prefix(old), new
    parts = rest.split(" ", 1)
    return _strip_prefix(parts[0]), _strip_prefix(parts[-1])


def _header_path(raw: str) -> str:
    """The path on a `---` or `+++` line, without its timestamp or `a/`/`b/`."""
    raw = raw.split("\t", 1)[0].rstrip()
    raw = _unquote(raw)
    return raw if raw == DEV_NULL else _strip_prefix(raw)


def _strip_prefix(path: str) -> str:
    path = _unquote(path)
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _take_quoted(text: str) -> tuple[str, int, str]:
    """A leading C-quoted string, decoded, and what follows it."""
    out: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(text[index : index + 2])
            index += 2
            continue
        if char == '"':
            return _decode_escapes("".join(out)), index, text[index + 1 :]
        out.append(char)
        index += 1
    return _decode_escapes("".join(out)), index, ""


def _unquote(path: str) -> str:
    path = path.strip()
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return _take_quoted(path)[0]
    return path


def _decode_escapes(text: str) -> str:
    """Git's C quoting: `\\t`, `\\"`, and octal bytes for anything non-ASCII."""
    raw = bytearray()
    index = 0
    simple = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            octal = text[index + 1 : index + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                raw.append(int(octal, 8) & 0xFF)
                index += 4
                continue
            if nxt in simple:
                raw.append(simple[nxt])
                index += 2
                continue
        raw.extend(char.encode("utf-8"))
        index += 1
    return raw.decode("utf-8", errors="replace")
