"""`plugins_settings.diffs`, read once per session with its defaults applied here.

The core fills declared defaults from `config_schema` in `PLUGIN.md`; they are
restated here because this module is also where a value is checked, and a
check needs the default it falls back to. `tests/test_diffs_settings.py` holds
the two in step.

A value that is the wrong type or out of range falls back to the default with a
note, never a refusal: a typo in `config.json` should cost one setting, not the
tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

LAYOUTS = ("unified", "split")
THEMES = ("light", "dark")
INDICATORS = ("bars", "classic", "none")
MODES = ("view", "file", "both")
FILE_FORMATS = ("png", "pdf")
FILE_QUALITIES = ("standard", "hq", "print")
SCALE_RANGE = (1, 4)
MAX_WIDTH_RANGE = (640, 2400)
TTL_RANGE = (60, 21_600)
FONT_SIZE_RANGE = (8, 32)
LINE_SPACING_RANGE = (1.0, 3.0)


@dataclass(frozen=True, slots=True)
class Settings:
    font_family: str = "Fira Code"
    font_size: int = 15
    line_spacing: float = 1.6
    layout: str = "unified"
    show_line_numbers: bool = True
    diff_indicators: str = "bars"
    word_wrap: bool = True
    background: bool = True
    theme: str = "dark"
    file_format: str = "png"
    file_quality: str = "standard"
    file_scale: int = 2
    file_max_width: int = 960
    mode: str = "both"
    ttl_seconds: int = 1800
    executable: str = ""

    @classmethod
    def read(cls, values: Mapping[str, Any] | None) -> tuple[Settings, list[str]]:
        """The settings, and a note for each value that was not usable."""
        values = values or {}
        base = cls()
        notes: list[str] = []

        def choice(key: str, options: tuple[str, ...]) -> str:
            default: str = getattr(base, key)
            raw = values.get(key, default)
            if isinstance(raw, str) and raw.strip().lower() in options:
                return raw.strip().lower()
            notes.append(
                f"setting {key}={raw!r} is not one of {', '.join(options)}; using {default!r}"
            )
            return default

        def number(key: str, bounds: tuple[int, int]) -> int:
            default: int = getattr(base, key)
            raw = values.get(key, default)
            if isinstance(raw, int) and not isinstance(raw, bool) and bounds[0] <= raw <= bounds[1]:
                return raw
            notes.append(
                f"setting {key}={raw!r} is not a whole number "
                f"{bounds[0]}-{bounds[1]}; using {default}"
            )
            return default

        def flag(key: str) -> bool:
            default: bool = getattr(base, key)
            raw = values.get(key, default)
            if isinstance(raw, bool):
                return raw
            notes.append(f"setting {key}={raw!r} is not true or false; using {default}")
            return default

        spacing_raw = values.get("line_spacing", base.line_spacing)
        low, high = LINE_SPACING_RANGE
        if (
            isinstance(spacing_raw, int | float)
            and not isinstance(spacing_raw, bool)
            and low <= float(spacing_raw) <= high
        ):
            spacing = float(spacing_raw)
        else:
            notes.append(
                f"setting line_spacing={spacing_raw!r} is not a number {low}-{high}; "
                f"using {base.line_spacing}"
            )
            spacing = base.line_spacing

        family_raw = values.get("font_family", base.font_family)
        if isinstance(family_raw, str) and family_raw.strip() and len(family_raw) <= 256:
            family = family_raw.strip()
        else:
            notes.append(
                f"setting font_family={family_raw!r} is not a font name; using {base.font_family!r}"
            )
            family = base.font_family

        executable_raw = values.get("executable", "")
        executable = executable_raw.strip() if isinstance(executable_raw, str) else ""
        if not isinstance(executable_raw, str):
            notes.append("setting executable is not a path; ignored")

        settings = cls(
            font_family=family,
            font_size=number("font_size", FONT_SIZE_RANGE),
            line_spacing=spacing,
            layout=choice("layout", LAYOUTS),
            show_line_numbers=flag("show_line_numbers"),
            diff_indicators=choice("diff_indicators", INDICATORS),
            word_wrap=flag("word_wrap"),
            background=flag("background"),
            theme=choice("theme", THEMES),
            file_format=choice("file_format", FILE_FORMATS),
            file_quality=choice("file_quality", FILE_QUALITIES),
            file_scale=number("file_scale", SCALE_RANGE),
            file_max_width=number("file_max_width", MAX_WIDTH_RANGE),
            mode=choice("mode", MODES),
            ttl_seconds=number("ttl_seconds", TTL_RANGE),
            executable=executable,
        )
        return settings, notes
