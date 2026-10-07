"""The diff as a PNG or a PDF: the static page, photographed by a headless Chromium.

The browser is found the way the bundled `browser` plugin finds one - a
configured `executable`, then the environment's usual variables, then Chrome,
Edge, Brave or Chromium where the platform keeps them, then Playwright's own
Chromium - and is launched on `scrubbed_environment()`, because it is a process
this plugin spawns.

It never touches the network. The page is handed over with `set_content`, every
request the page could make is aborted, the page's own policy refuses every
fetch, and Chromium is pointed at a proxy that does not exist so that nothing it
does in the background reaches anywhere either. One browser per call, closed in
a `finally`: a cancelled call kills it rather than leaving it running.

Playwright is imported here and only here, lazily, so the plugin loads and the
`view` mode works on an install that never added the `render` extra.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultron.sdk.runtime import scrubbed_environment

QUALITY_PRESETS: dict[str, tuple[int, int]] = {
    "standard": (2, 8_000_000),
    "hq": (3, 14_000_000),
    "print": (4, 24_000_000),
}
"""Each quality's default scale and the most pixels its PNG may be."""

MAX_PDF_PAGES = 50
PDF_PAGE_RATIO = math.sqrt(2)
"""A page as tall as ISO paper is for its width."""

EXECUTABLE_ENV = (
    "ULTRON_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
)
NO_NETWORK_ARGS = (
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-extensions",
    "--disable-sync",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-pings",
)
"""Port 9 is discard: a proxy that refuses every connection is a network that
does not exist, which is the one this browser is meant to have."""

PAGE_POLICY = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; img-src data:; font-src data:\">"
)
"""The same policy the core puts on a view, put on the page a file is made from."""

RENDER_TIMEOUT_MS = 60_000


class RenderError(Exception):
    """A file could not be made. The message is for the model."""


@dataclass(frozen=True, slots=True)
class Rendered:
    data: bytes
    file_format: str
    width: int
    height: int
    scale: int
    pages: int = 0


@dataclass(frozen=True, slots=True)
class Browser:
    """A browser to launch. An empty path means Playwright's own Chromium."""

    label: str
    path: str = ""


# -- finding a browser -------------------------------------------------------


def playwright_missing() -> str:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return (
            "PNG and PDF need Playwright, which is not installed - "
            "pip install playwright into Ultron's environment, then, if no Chrome, "
            "Edge, Brave or Chromium is installed, playwright install chromium"
        )
    return ""


def _candidates() -> list[Browser]:
    found: list[Browser] = []
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for label, tail in (
            ("Chrome", r"Google\Chrome\Application\chrome.exe"),
            ("Edge", r"Microsoft\Edge\Application\msedge.exe"),
            ("Brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            ("Chromium", r"Chromium\Application\chrome.exe"),
        ):
            found.extend(Browser(label, str(Path(root) / tail)) for root in roots if root)
    elif sys.platform == "darwin":
        for label, app in (
            ("Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
        ):
            found.append(Browser(label, f"/Applications/{app}"))
    else:
        for label, name in (
            ("Chrome", "google-chrome"),
            ("Chrome", "google-chrome-stable"),
            ("Edge", "microsoft-edge"),
            ("Brave", "brave-browser"),
            ("Brave", "brave"),
            ("Chromium", "chromium"),
            ("Chromium", "chromium-browser"),
        ):
            located = shutil.which(name)
            if located:
                found.append(Browser(label, located))
    return found


def find_browser(executable: str = "") -> Browser:
    """The configured `executable`, then the environment, then the platform's
    usual places, then Playwright's own Chromium. A configured path that is not
    a file is an error rather than a fallback: somebody chose it."""
    if executable:
        if Path(executable).is_file():
            return Browser(Path(executable).name, executable)
        raise RenderError(f"setting executable={executable!r} is not a file")
    for variable in EXECUTABLE_ENV:
        value = os.environ.get(variable, "").strip()
        if value and Path(value).is_file():
            return Browser(Path(value).name, value)
    for candidate in _candidates():
        if Path(candidate.path).is_file():
            return candidate
    return Browser("Playwright Chromium")


# -- rendering ---------------------------------------------------------------


def with_policy(page: str) -> str:
    """The page with its own no-fetch policy first in `<head>`."""
    return re.sub(r"<head>", "<head>" + PAGE_POLICY, page, count=1)


async def render_file(
    page: str,
    *,
    file_format: str,
    scale: int,
    max_width: int,
    quality: str,
    executable: str = "",
) -> Rendered:
    """Photograph a static page. Raises `RenderError` with a reason the model
    can act on - too big, no browser, no Playwright."""
    missing = playwright_missing()
    if missing:
        raise RenderError(missing)
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    browser_found = find_browser(executable)
    _, max_pixels = QUALITY_PRESETS[quality]
    # Playwright's annotation is invariant in the value type; the values are strings.
    environment: dict[str, str | float | bool] = dict(scrubbed_environment())
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    executable_path=browser_found.path or None,
                    headless=True,
                    env=environment,
                    args=list(NO_NETWORK_ARGS),
                    timeout=30_000,
                )
            except PlaywrightError as error:
                raise RenderError(_launch_failure(browser_found, error)) from None
            try:
                return await _photograph(
                    browser,
                    page,
                    file_format=file_format,
                    scale=scale,
                    max_width=max_width,
                    max_pixels=max_pixels,
                )
            finally:
                # Reached on cancellation too: a call that was stopped does not
                # leave a browser behind it.
                with contextlib.suppress(Exception):
                    await browser.close()
    except PlaywrightError as error:
        raise RenderError(f"rendering failed: {_first_line(error)}") from None


async def _photograph(
    browser: Any,
    page_html: str,
    *,
    file_format: str,
    scale: int,
    max_width: int,
    max_pixels: int,
) -> Rendered:
    context = await browser.new_context(
        viewport={"width": max_width, "height": 800},
        device_scale_factor=scale if file_format == "png" else 1,
        offline=True,
        java_script_enabled=True,  # for measuring only; the page carries no script
        bypass_csp=False,
    )
    context.set_default_timeout(RENDER_TIMEOUT_MS)

    async def refuse(route: Any) -> None:
        await route.abort()

    await context.route("**/*", refuse)
    tab = await context.new_page()
    await tab.set_content(with_policy(page_html), wait_until="load")
    await tab.emulate_media(media="screen")
    height = int(await tab.evaluate("document.documentElement.scrollHeight"))
    width = int(await tab.evaluate("document.documentElement.scrollWidth"))
    width = max(width, max_width)

    if file_format == "png":
        pixels = width * scale * height * scale
        if pixels > max_pixels:
            raise RenderError(
                f"the PNG would be {width * scale}x{height * scale} "
                f"({pixels / 1_000_000:.1f} MP), over this quality's "
                f"{max_pixels // 1_000_000} MP - lower file_scale, use a higher file_quality, "
                "or ask for file_format=pdf, which pages a long diff"
            )
        data = await tab.screenshot(full_page=True, type="png", animations="disabled")
        return Rendered(data, "png", width * scale, height * scale, scale)

    page_height = math.ceil(width * PDF_PAGE_RATIO)
    pages = max(1, math.ceil(height / page_height))
    if pages > MAX_PDF_PAGES:
        raise RenderError(
            f"the PDF would be about {pages} pages, over {MAX_PDF_PAGES} - "
            "send a smaller patch, or split it by file"
        )
    data = await tab.pdf(
        width=f"{width}px",
        height=f"{page_height}px",
        print_background=True,
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
    )
    counted = len(re.findall(rb"/Type\s*/Page(?![s\w])", data)) or pages
    if counted > MAX_PDF_PAGES:
        raise RenderError(f"the PDF came to {counted} pages, over {MAX_PDF_PAGES}")
    return Rendered(data, "pdf", width, height, scale, counted)


def _launch_failure(found: Browser, error: Exception) -> str:
    text = _first_line(error)
    if not found.path and "Executable doesn't exist" in str(error):
        return (
            "no browser to render with - install Chrome, Edge, Brave or Chromium, "
            "run playwright install chromium, or set plugins_settings.diffs.executable"
        )
    return f"could not start {found.label}: {text}"


def _first_line(error: Exception) -> str:
    return (str(error).strip().splitlines() or [type(error).__name__])[0][:300]
