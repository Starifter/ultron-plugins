"""Load the plugin the way Ultron does - `plugin.py` by path, under its own name -
so the tests import `ultron_plugin_discord`, and give them the disposable home
Ultron's own suite runs every test in."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugin.py"

if "ultron_plugin_discord" not in sys.modules:
    spec = importlib.util.spec_from_file_location("ultron_plugin_discord", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


@pytest.fixture
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point `~` somewhere disposable: the adapter reads `~/.ultron/.env`, and no
    test may read the real one."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    yield home
