"""The himalaya plugin: it registers one skill, and that skill is the one in `skill/SKILL.md`.

Run from a checkout of Ultron (`uv run pytest path/to/himalaya/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ultron.skills.base import read_manifest

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_himalaya", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()


def test_it_registers_the_one_skill_its_contract_names() -> None:
    offered: list[Any] = []
    ctx = SimpleNamespace(register_skill=offered.append)
    plugin.HimalayaPlugin().register(ctx)
    assert [skill.name for skill in offered] == ["himalaya"]


def test_the_code_says_what_the_skill_file_says() -> None:
    # The SKILL.md stays a skill a person can copy into ~/.ultron/skills/; the
    # plugin restates its frontmatter, and this is what keeps the two one.
    built = plugin.skill()
    read = read_manifest(plugin.SKILL_DIR / "SKILL.md", source="plugin")
    assert not read.error, read.error
    assert built.name == read.name
    assert built.description == read.description
    assert built.requires == read.requires
    assert built.emoji == read.emoji
    assert built.homepage == read.homepage


def test_the_body_loads_from_the_plugin() -> None:
    body = plugin.skill().body()
    assert body.lstrip().startswith("# ")
    assert "name: himalaya" not in body  # the frontmatter is stripped


def test_its_reference_is_a_linked_file() -> None:
    assert plugin.skill().linked_files() == ["references/configuration.md"]
