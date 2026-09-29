"""Artifacts: made, served while they live, never by a crafted id, swept after."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from ultron_plugin_diffs_lib.store import ID_PATTERN, MAX_AGE_SECONDS, DiffStore, valid_id


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_create_and_read_while_it_lives(tmp_path: Path) -> None:
    clock = Clock()
    store = DiffStore(tmp_path, clock=clock)
    artifact = store.create({"title": "t", "additions": 1}, 60, "<p>page</p>")
    assert ID_PATTERN.match(artifact.id)
    assert artifact.directory == tmp_path / ".ultron" / "diffs" / artifact.id
    assert (artifact.directory / "view.html.gz").is_file()
    found = store.read_view(artifact.id)
    assert found is not None
    page, meta = found
    assert page == "<p>page</p>"
    assert meta["title"] == "t" and meta["additions"] == 1
    assert meta["expires_at"].endswith("Z")

    clock.now += 61
    assert store.read_view(artifact.id) is None
    assert store.meta(artifact.id) is None


def test_file_only_has_no_view(tmp_path: Path) -> None:
    store = DiffStore(tmp_path)
    artifact = store.create({}, 60, None)
    assert store.meta(artifact.id) is not None
    assert store.read_view(artifact.id) is None


@pytest.mark.parametrize(
    "crafted",
    [
        "../../etc/passwd",
        "..",
        "",
        "a" * 21,
        "a" * 23,
        "aaaaaaaaaaaaaaaaaaaa/.",
        "aaaaaaaaaaaaaaaaaaaaa\\",
        "C:\\Windows\\system32xx",
        "%2e%2e%2f%2e%2e%2fxxxxxx",
    ],
)
def test_crafted_ids_are_not_found(tmp_path: Path, crafted: str) -> None:
    store = DiffStore(tmp_path)
    assert not valid_id(crafted)
    assert store.read_view(crafted) is None
    assert store.meta(crafted) is None


def test_a_valid_looking_id_outside_the_root_is_not_followed(tmp_path: Path) -> None:
    store = DiffStore(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "meta.json").write_text(json.dumps({"expires": 9e18}), encoding="utf-8")
    store.root.mkdir(parents=True)
    link = store.root / ("A" * 22)
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert store.meta("A" * 22) is None
    assert store.sweep() == []
    assert outside.is_dir()


def test_sweep_removes_expired_and_old_and_nothing_else(tmp_path: Path) -> None:
    clock = Clock()
    store = DiffStore(tmp_path, clock=clock)
    short = store.create({}, 60, "x")
    long = store.create({}, 21_600, "x")
    keep = store.create({}, 60, "x")
    stranger = store.root / "not-an-id"
    stranger.mkdir()
    clock.now += 120
    removed = store.sweep(keep=keep.id)
    assert removed == [short.id]
    assert long.directory.is_dir() and keep.directory.is_dir() and stranger.is_dir()


def test_sweep_removes_anything_older_than_a_day(tmp_path: Path) -> None:
    clock = Clock()
    store = DiffStore(tmp_path, clock=clock)
    artifact = store.create({}, 21_600, "x")
    # A record that claims to live forever still goes after a day.
    store.update_meta(artifact, expires=9e18)
    clock.now += MAX_AGE_SECONDS + 1
    assert store.sweep() == [artifact.id]


def test_sweep_spares_a_fresh_directory_with_no_meta(tmp_path: Path) -> None:
    store = DiffStore(tmp_path)
    in_flight = store.root / ("B" * 22)
    in_flight.mkdir(parents=True)
    assert store.sweep() == []
    old = os.stat(in_flight).st_mtime - MAX_AGE_SECONDS - 10
    os.utime(in_flight, (old, old))
    assert store.sweep() == ["B" * 22]


def test_corrupt_view_is_not_found(tmp_path: Path) -> None:
    store = DiffStore(tmp_path)
    artifact = store.create({}, 60, "x")
    (artifact.directory / "view.html.gz").write_bytes(b"not gzip")
    assert store.read_view(artifact.id) is None
