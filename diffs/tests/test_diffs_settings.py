"""Settings: the code's defaults are the manifest's, and a bad value is a note."""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from ultron_plugin_diffs_lib.settings import Settings

MANIFEST = Path(__file__).resolve().parents[1] / "PLUGIN.md"


def manifest_defaults() -> dict[str, str]:
    """`config_schema` defaults, read without a YAML library: the manifest is
    ours and its shape is fixed - a key at two spaces, `default:` at four."""
    text = MANIFEST.read_text(encoding="utf-8").split("config_schema:", 1)[1].split("\n---", 1)[0]
    found: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        top = re.match(r"^  (\w+):\s*$", line)
        if top:
            key = top.group(1)
            continue
        default = re.match(r"^    default:\s*(.*)$", line)
        if default and key:
            found[key] = default.group(1).strip()
    return found


def test_manifest_and_code_agree() -> None:
    declared = manifest_defaults()
    base = Settings()
    assert set(declared) == {f.name for f in fields(Settings)}
    for name, raw in declared.items():
        value = getattr(base, name)
        if isinstance(value, bool):
            assert raw == str(value).lower(), name
        elif raw == '""':
            assert value == "", name
        else:
            assert raw == str(value), name


def test_defaults_with_nothing_configured() -> None:
    settings, notes = Settings.read(None)
    assert settings == Settings()
    assert notes == []


def test_good_values_are_taken() -> None:
    settings, notes = Settings.read(
        {
            "layout": "Split",
            "theme": "light",
            "file_scale": 3,
            "line_spacing": 2,
            "word_wrap": False,
        }
    )
    assert notes == []
    assert (settings.layout, settings.theme, settings.file_scale) == ("split", "light", 3)
    assert settings.line_spacing == 2.0 and settings.word_wrap is False


def test_bad_values_fall_back_with_notes() -> None:
    settings, notes = Settings.read(
        {
            "layout": "diagonal",
            "file_scale": 9,
            "ttl_seconds": True,
            "show_line_numbers": "yes",
            "line_spacing": "wide",
            "font_family": "",
            "executable": 7,
        }
    )
    assert settings.layout == "unified"
    assert settings.file_scale == 2
    assert settings.ttl_seconds == 1800
    assert settings.show_line_numbers is True
    assert settings.line_spacing == 1.6
    assert settings.font_family == "Fira Code"
    assert settings.executable == ""
    assert len(notes) == 7
