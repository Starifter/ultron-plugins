"""PNG and PDF, through a real headless Chromium - skipped where there is none."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ultron_plugin_diffs_lib import files
from ultron_plugin_diffs_lib.engine import from_texts
from ultron_plugin_diffs_lib.files import (
    EXECUTABLE_ENV,
    RenderError,
    find_browser,
    playwright_missing,
    render_file,
    with_policy,
)
from ultron_plugin_diffs_lib.html import RenderOptions, render

PAGE = render(from_texts("a = 1\n", "a = 2\n", "x.py"), RenderOptions(static=True))


async def _render(**overrides: object) -> files.Rendered:
    arguments: dict[str, object] = {
        "file_format": "png",
        "scale": 1,
        "max_width": 800,
        "quality": "standard",
    }
    arguments.update(overrides)
    page = str(arguments.pop("page", PAGE))
    try:
        return await render_file(page, **arguments)  # type: ignore[arg-type]
    except RenderError as error:
        text = str(error)
        if "Playwright" in text or "no browser" in text or "could not start" in text:
            pytest.skip(f"no renderer here: {text}")
        raise


def test_missing_playwright_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    message = playwright_missing()
    assert "pip install playwright" in message and "playwright install chromium" in message


def test_configured_executable_must_exist(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="not a file"):
        find_browser(str(tmp_path / "nope"))
    real = tmp_path / "chrome.exe"
    real.write_bytes(b"")
    assert find_browser(str(real)).path == str(real)


def test_environment_is_read_before_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "browser.exe"
    fake.write_bytes(b"")
    for variable in EXECUTABLE_ENV:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", str(fake))
    assert find_browser().path == str(fake)
    monkeypatch.setenv("ULTRON_BROWSER_EXECUTABLE_PATH", str(tmp_path / "missing"))
    assert find_browser().path == str(fake)  # a variable naming nothing is skipped


def test_the_page_gets_its_own_no_fetch_policy() -> None:
    page = with_policy(PAGE)
    assert page.index("Content-Security-Policy") < page.index("<style>")
    assert "default-src 'none'" in page


async def test_png() -> None:
    rendered = await _render()
    assert rendered.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert rendered.width == 800 and rendered.file_format == "png"


async def test_png_scale_multiplies_pixels() -> None:
    rendered = await _render(scale=2)
    assert rendered.width == 1600


async def test_pdf() -> None:
    rendered = await _render(file_format="pdf")
    assert rendered.data.startswith(b"%PDF")
    assert rendered.pages == 1


async def test_over_the_pixel_cap_is_refused() -> None:
    tall = "".join(f"line {i}\n" for i in range(3000))
    page = render(from_texts("", tall), RenderOptions(static=True))
    with pytest.raises(RenderError, match="MP"):
        await _render(page=page, scale=4, max_width=2400, quality="standard")


async def test_over_the_page_cap_is_refused() -> None:
    tall = "".join(f"line {i}\n" for i in range(6000))
    page = render(from_texts("", tall), RenderOptions(static=True))
    with pytest.raises(RenderError, match="pages"):
        await _render(page=page, file_format="pdf", max_width=640)


async def test_the_page_reaches_nothing(tmp_path: Path) -> None:
    """A page that tries to load something gets nothing - the request is
    refused before it leaves, so rendering still succeeds, offline."""
    hostile = PAGE.replace(
        "</body>",
        '<img src="http://127.0.0.1:9/x.png"><link rel="stylesheet" href="https://example.com/a.css">'
        "</body>",
    )
    rendered = await _render(page=hostile)
    assert rendered.data.startswith(b"\x89PNG")


@pytest.mark.skipif(os.name != "nt", reason="the Windows candidates")
def test_windows_candidates_include_edge_and_brave() -> None:
    labels = {candidate.label for candidate in files._candidates()}
    assert {"Chrome", "Edge", "Brave", "Chromium"} <= labels
