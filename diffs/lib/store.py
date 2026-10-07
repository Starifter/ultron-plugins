"""Where a diff is kept between the call that made it and the person who opens it.

`<workspace>/.ultron/diffs/<id>/`, one directory per call:

- `view.html.gz` - the page, gzipped; absent for a `file`-only call
- `meta.json`    - when it was made and expires, the title, the counts
- `diff.png` / `diff.pdf` - the rendered file, when one was asked for

The id is `secrets.token_urlsafe(16)` and is the only thing a reader hands in,
so it is checked against exactly that shape before it is joined to a path - a
crafted id is not found, never resolved. An artifact past its `expires` is not
served, and is deleted the next time anything is made; a directory older than a
day goes too, whatever its `meta.json` says or whether it has one.
"""

from __future__ import annotations

import gzip
import json
import re
import secrets
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")
"""What `token_urlsafe(16)` makes: 22 characters of the URL-safe alphabet."""

VIEW_FILE = "view.html.gz"
META_FILE = "meta.json"
MAX_AGE_SECONDS = 24 * 60 * 60
"""Swept regardless of its own expiry: nothing a call can ask for lives longer."""


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_id(artifact_id: str) -> bool:
    return isinstance(artifact_id, str) and bool(ID_PATTERN.match(artifact_id))


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    directory: Path
    created: float
    expires: float

    @property
    def expires_at(self) -> str:
        return iso(self.expires)


class DiffStore:
    """One workspace's diffs. Cheap to build; holds nothing but the path."""

    def __init__(self, workspace: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.workspace = Path(workspace)
        self.root = self.workspace / ".ultron" / "diffs"
        self._clock = clock

    # -- writing -------------------------------------------------------------

    def create(self, meta: Mapping[str, Any], ttl_seconds: int, html: str | None) -> Artifact:
        """A new artifact directory with its meta and, for a view, its page."""
        now = self._clock()
        artifact_id = secrets.token_urlsafe(16)
        directory = self.root / artifact_id
        directory.mkdir(parents=True, exist_ok=False)
        artifact = Artifact(artifact_id, directory, now, now + ttl_seconds)
        record = {
            **meta,
            "id": artifact_id,
            "created": now,
            "expires": artifact.expires,
            "created_at": iso(now),
            "expires_at": artifact.expires_at,
            "view": html is not None,
        }
        if html is not None:
            (directory / VIEW_FILE).write_bytes(gzip.compress(html.encode("utf-8"), 6))
        (directory / META_FILE).write_text(json.dumps(record, indent=2), encoding="utf-8")
        return artifact

    def update_meta(self, artifact: Artifact, **fields: Any) -> None:
        path = artifact.directory / META_FILE
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(fields)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def remove(self, artifact_id: str) -> None:
        directory = self._directory(artifact_id)
        if directory is not None:
            _remove(directory)

    # -- reading -------------------------------------------------------------

    def meta(self, artifact_id: str) -> dict[str, Any] | None:
        """An artifact's record, or `None` if the id is malformed, unknown or
        expired. Expired is not found: a link that outlived its diff opens
        nothing rather than something stale."""
        directory = self._directory(artifact_id)
        if directory is None:
            return None
        try:
            record = json.loads((directory / META_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        expires = record.get("expires")
        if not isinstance(expires, int | float) or expires <= self._clock():
            return None
        return record

    def read_view(self, artifact_id: str) -> tuple[str, dict[str, Any]] | None:
        """The page and its record, while it lives."""
        record = self.meta(artifact_id)
        if record is None:
            return None
        directory = self.root / artifact_id
        try:
            page = gzip.decompress((directory / VIEW_FILE).read_bytes()).decode("utf-8")
        except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError):
            return None
        return page, record

    def _directory(self, artifact_id: str) -> Path | None:
        """The directory an id names, only if the id is exactly the shape we
        make and the directory is a real one directly under the root."""
        if not valid_id(artifact_id):
            return None
        directory = self.root / artifact_id
        if directory.is_symlink() or not directory.is_dir():
            return None
        try:
            if directory.resolve().parent != self.root.resolve():
                return None
        except OSError:
            return None
        return directory

    # -- sweeping ------------------------------------------------------------

    def sweep(self, keep: str = "") -> list[str]:
        """Delete every expired artifact and every one older than a day.

        Only directories whose names are ids this store could have made are
        touched: anything else under `.ultron/diffs/` belongs to somebody who
        put it there on purpose."""
        removed: list[str] = []
        now = self._clock()
        try:
            children = list(self.root.iterdir())
        except OSError:
            return removed
        for child in children:
            name = child.name
            if name == keep or not valid_id(name):
                continue
            if child.is_symlink() or not child.is_dir():
                continue
            if self._stale(child, now):
                _remove(child)
                removed.append(name)
        return removed

    def _stale(self, directory: Path, now: float) -> bool:
        try:
            if now - directory.stat().st_mtime > MAX_AGE_SECONDS:
                return True
            record = json.loads((directory / META_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No readable record: gone once a day has passed, and not before,
            # because a create in flight has a directory before it has a meta.
            return False
        created = record.get("created") if isinstance(record, dict) else None
        expires = record.get("expires") if isinstance(record, dict) else None
        if isinstance(created, int | float) and now - created > MAX_AGE_SECONDS:
            return True
        return not isinstance(expires, int | float) or expires <= now


def _remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
