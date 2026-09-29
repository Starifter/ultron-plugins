"""The diff model: before/after, patches of every shape git writes, and the limits."""

from __future__ import annotations

import pytest
from ultron_plugin_diffs_lib.engine import (
    MAX_PATCH_FILES,
    MAX_PATCH_LINES,
    DiffError,
    from_texts,
    parse_patch,
    split_lines,
)

MULTI = """\
diff --git a/src/a.py b/src/b.py
similarity index 90%
rename from src/a.py
rename to src/b.py
index 1111111..2222222 100644
--- a/src/a.py
+++ b/src/b.py
@@ -10,3 +10,4 @@ def foo():
 x = 1
-y = 2
+y = 3
+z = 4
 w = 5
diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+-- not a header
diff --git a/gone.md b/gone.md
deleted file mode 100644
--- a/gone.md
+++ /dev/null
@@ -1 +0,0 @@
-bye
\\ No newline at end of file
diff --git a/moved.txt b/elsewhere.txt
similarity index 100%
rename from moved.txt
rename to elsewhere.txt
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
"""


def test_before_after_counts_and_numbers() -> None:
    diffs = from_texts("a\nb\nc\n", "a\nB\nc\nd\n", "x.txt")
    assert diffs.input_kind == "before_after"
    (diff,) = diffs.files
    assert diff.path == "x.txt"
    assert (diff.additions, diff.deletions) == (2, 1)
    assert diffs.changed
    kinds = [(line.kind, line.text, line.old_no, line.new_no) for line in diff.hunks[0].lines]
    assert kinds == [
        ("context", "a", 1, 1),
        ("del", "b", 2, None),
        ("add", "B", None, 2),
        ("context", "c", 3, 3),
        ("add", "d", None, 4),
    ]
    assert diff.old_text == "a\nb\nc\n"


def test_identical_texts_are_unchanged() -> None:
    diffs = from_texts("same\n", "same\n")
    assert not diffs.changed
    assert diffs.files[0].path == "untitled"


def test_line_endings_do_not_make_a_change() -> None:
    assert split_lines("a\r\nb\rc\n") == ["a", "b", "c"]
    assert not from_texts("a\r\nb\r\n", "a\nb\n").changed


def test_form_feed_is_not_a_line_break() -> None:
    assert split_lines("a\x0cb\n") == ["a\x0cb"]


def test_multi_file_patch() -> None:
    diffs = parse_patch(MULTI)
    assert diffs.input_kind == "patch"
    by_path = {f.path: f for f in diffs.files}
    assert list(by_path) == ["src/b.py", "new.txt", "gone.md", "elsewhere.txt", "img.png", "run.sh"]

    renamed = by_path["src/b.py"]
    assert (renamed.status, renamed.old_path, renamed.new_path) == (
        "renamed",
        "src/a.py",
        "src/b.py",
    )
    assert (renamed.additions, renamed.deletions) == (2, 1)
    hunk = renamed.hunks[0]
    assert (hunk.old_start, hunk.new_start, hunk.section) == (10, 10, "def foo():")
    assert hunk.lines[-1].old_no == 12 and hunk.lines[-1].new_no == 13

    added = by_path["new.txt"]
    assert added.status == "added"
    # Read by count: a `+-- ...` line is an addition, not a header.
    assert [line.text for line in added.hunks[0].lines] == ["hello", "-- not a header"]

    deleted = by_path["gone.md"]
    assert (deleted.status, deleted.deletions) == ("deleted", 1)

    pure_rename = by_path["elsewhere.txt"]
    assert (pure_rename.status, pure_rename.old_path, pure_rename.hunks) == (
        "renamed",
        "moved.txt",
        [],
    )
    assert pure_rename.changed

    assert by_path["img.png"].binary
    assert by_path["run.sh"].mode_change == "100644 -> 100755"
    assert (diffs.additions, diffs.deletions) == (4, 2)


def test_plain_diff_u_without_git_headers() -> None:
    patch = (
        "--- old/a.c\t2024-01-01 00:00:00\n"
        "+++ new/a.c\t2024-01-02 00:00:00\n"
        "@@ -1,2 +1,2 @@\n"
        "-int x;\n"
        "+long x;\n"
        " int y;\n"
        "--- b.c\n"
        "+++ b.c\n"
        "@@ -1 +1 @@\n"
        "-1\n"
        "+2\n"
    )
    diffs = parse_patch(patch)
    assert [f.new_path for f in diffs.files] == ["new/a.c", "b.c"]
    assert diffs.files[0].status == "renamed"  # the two names differ
    assert diffs.files[1].status == "modified"


def test_hunk_with_omitted_counts_and_stripped_blank_context() -> None:
    patch = "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n"
    (diff,) = parse_patch(patch).files
    assert [line.kind for line in diff.hunks[0].lines] == ["context", "context", "del", "add"]


def test_quoted_paths() -> None:
    patch = (
        'diff --git "a/sp ace\\303\\251.txt" "b/sp ace\\303\\251.txt"\n'
        '--- "a/sp ace\\303\\251.txt"\n'
        '+++ "b/sp ace\\303\\251.txt"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    (diff,) = parse_patch(patch).files
    assert diff.path == "sp aceé.txt"


def test_unquoted_path_with_space() -> None:
    patch = "diff --git a/my file.txt b/my file.txt\nnew file mode 100644\n"
    (diff,) = parse_patch(patch).files
    assert (diff.path, diff.status) == ("my file.txt", "added")


def test_not_a_patch_is_an_error() -> None:
    with pytest.raises(DiffError, match="no file diffs"):
        parse_patch("just some words\n")


def test_too_many_files() -> None:
    one = "diff --git a/f{0} b/f{0}\n--- a/f{0}\n+++ b/f{0}\n@@ -1 +1 @@\n-a\n+b\n"
    patch = "".join(one.format(i) for i in range(MAX_PATCH_FILES + 1))
    with pytest.raises(DiffError, match=f"more than {MAX_PATCH_FILES} files"):
        parse_patch(patch)
    # Exactly at the limit is fine.
    parse_patch("".join(one.format(i) for i in range(MAX_PATCH_FILES)))


def test_too_many_lines() -> None:
    patch = "--- a/x\n+++ b/x\n" + "+\n" * MAX_PATCH_LINES
    with pytest.raises(DiffError, match="lines"):
        parse_patch(patch)
