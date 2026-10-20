"""Exa, through its search API: a web_search backend that searches by meaning."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ultron.sdk.auth import SecretRef, read_dotenv, resolve
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import CredentialError, NetworkPolicyError
from ultron.sdk.web import Answer, Found, Query, Result, check_url, post

DEFAULT_BASE_URL = "https://api.exa.ai"
MAX_COUNT = 100
TYPE_SHAPE = re.compile(r"^[a-z][a-z-]{0,31}$")
"""A search type is Exa's word - auto, lite, deep-lite, deep, or a legacy one
Exa still takes - and anything shaped like a word is passed through, because
Exa accepts types this plugin has not heard of."""
RESPONSE_BYTES = 16_000_000
"""OpenClaw's cap on Exa's reply: page text can make it large."""


def endpoint_of(base_url: str) -> str:
    """OpenClaw's normalisation: a bare host gets `https://`, and `/search` is
    appended unless the path already ends there. Checked for shape, never for
    where it points - that is the client's question, asked on every request."""
    configured = (base_url or "").strip() or DEFAULT_BASE_URL
    if re.match(r"^[a-z][a-z0-9+.-]*://", configured, re.IGNORECASE) is None:
        configured = f"https://{configured}"
    checked = check_url(configured)
    path = checked.split("?", 1)[0].rstrip("/")
    return path if path.endswith("/search") else f"{path}/search"


def freshness_start(freshness: str, today: date) -> str:
    """OpenClaw's freshness as a start date: a day, a week, a month, a year back."""
    if freshness == "day":
        start = today - timedelta(days=1)
    elif freshness == "week":
        start = today - timedelta(days=7)
    elif freshness == "month":
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
        start = date(year, month, min(today.day, last))
    else:
        try:
            start = today.replace(year=today.year - 1)
        except ValueError:  # 29 February, a year after which there is none
            start = today.replace(year=today.year - 1, day=28)
    return start.isoformat()


class Exa:
    name = "exa"
    priority = 50
    """Beside Brave: a per-search price."""

    def __init__(
        self,
        *,
        workspace: Path | str = ".",
        api_key_env: str = "EXA_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        search_type: str = "auto",
        category: str = "",
        country: str = "",
        highlights: bool = True,
        summary: bool = False,
        text_max_characters: int = 0,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.ref = SecretRef("env", api_key_env or "EXA_API_KEY")
        self.base_url = base_url
        kind = search_type.strip().lower()
        self.search_type = kind if TYPE_SHAPE.match(kind) else "auto"
        self.category = category.strip()
        self.country = country.strip().upper()
        self.highlights = bool(highlights)
        self.summary = bool(summary)
        self.text_max_characters = max(0, int(text_max_characters))
        self.timeout = max(1.0, float(timeout_seconds))

    def ready(self) -> bool:
        """Whether a key can be had. Read fresh, never cached at install."""
        try:
            return bool(self._key())
        except CredentialError:
            return False

    def _key(self) -> str:
        return resolve(self.ref, dotenv=read_dotenv(self.workspace))

    def contents(self) -> dict[str, Any]:
        """What Exa reads out of each page. OpenClaw's default is highlights."""
        contents: dict[str, Any] = {}
        if self.highlights:
            contents["highlights"] = True
        if self.summary:
            contents["summary"] = {}
        if self.text_max_characters:
            contents["text"] = {"maxCharacters": self.text_max_characters}
        return contents or {"highlights": True}

    def body(self, query: Query, *, today: date | None = None) -> dict[str, Any]:
        """The request Exa is sent for this query, without the key."""
        body: dict[str, Any] = {
            "query": query.text,
            "numResults": max(1, min(query.count, MAX_COUNT)),
            "type": self.search_type,
            "contents": self.contents(),
        }
        domains = query.domains or ((query.site,) if query.site else ())
        if domains:
            body["includeDomains"] = list(domains)
        if query.excluded:
            body["excludeDomains"] = list(query.excluded)
        if query.after or query.before:
            if query.after:
                body["startPublishedDate"] = query.after
            if query.before:
                body["endPublishedDate"] = query.before
        elif query.freshness:
            body["startPublishedDate"] = freshness_start(
                query.freshness, today or datetime.now(UTC).date()
            )
        if self.category:
            body["category"] = self.category
        country = self.country or query.country.upper()
        if len(country) == 2:
            body["userLocation"] = country
        return body

    async def search(self, query: Query) -> Found:
        key = self._key()
        try:
            endpoint = endpoint_of(self.base_url)
        except NetworkPolicyError as error:
            raise RuntimeError(f"Exa's base_url is not usable: {error}") from None
        response = await post(
            endpoint,
            json=self.body(query),
            headers={
                "Accept": "application/json",
                "x-api-key": key,
                "x-exa-integration": "ultron",
            },
            max_bytes=RESPONSE_BYTES,
            timeout=self.timeout,
        )
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} from Exa: {_why(response.body)}")
        if response.truncated:
            raise RuntimeError(f"Exa's reply was larger than {RESPONSE_BYTES} bytes")
        try:
            payload = json.loads(response.body)
        except ValueError as error:
            raise RuntimeError(f"Exa returned something that is not JSON: {error}") from None
        if not isinstance(payload, dict):
            raise RuntimeError("Exa returned something that is not an object")
        rows = _rows(payload)[: max(1, min(query.count, MAX_COUNT))]
        return Found(results=tuple(rows), answer=_synthesis(payload, rows))


def _rows(payload: dict[str, Any]) -> list[Result]:
    """Each result, its snippet resolved as OpenClaw resolves it: the
    highlights, else the summary, else the page text."""
    rows: list[Result] = []
    items = payload.get("results", [])
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        highlights = item.get("highlights")
        said = (
            [h.strip() for h in highlights if isinstance(h, str) and h.strip()]
            if isinstance(highlights, list)
            else []
        )
        snippet = (
            " ... ".join(said)
            or str(item.get("summary", "") or "").strip()
            or str(item.get("text", "") or "").strip()
        )
        rows.append(
            Result(
                title=str(item.get("title", "") or ""),
                url=str(item.get("url", "") or ""),
                snippet=snippet,
                published=str(item.get("publishedDate", "") or "")[:10],
            )
        )
    return rows


def _synthesis(payload: dict[str, Any], rows: list[Result]) -> Answer | None:
    """A deep search's written output, when it wrote text. Cited by the pages it
    returned: Exa's grounding names them, and the URL is the part that can be
    checked."""
    output = payload.get("output")
    content = output.get("content") if isinstance(output, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None
    return Answer(
        text=content.strip(), citations=tuple(dict.fromkeys(r.url for r in rows if r.url))
    )


def _why(body: bytes) -> str:
    """Exa's error text - `{"error": ..., "tag": ...}` - without printing a page."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            tag = data.get("tag")
            if isinstance(error, str) and error:
                return f"{error} ({tag})" if isinstance(tag, str) and tag else error
            if isinstance(error, dict):
                return str(error.get("message") or "")
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class ExaPlugin(Plugin):
    name = "exa"
    description = "Exa as a web_search backend."

    def register(self, ctx: PluginContext) -> None:
        workspace = ctx.workspace
        settings = {
            key: str(ctx.setting(key, default))
            for key, default in (
                ("api_key_env", "EXA_API_KEY"),
                ("base_url", DEFAULT_BASE_URL),
                ("search_type", "auto"),
                ("category", ""),
                ("country", ""),
            )
        }
        highlights = bool(ctx.setting("highlights", True))
        summary = bool(ctx.setting("summary", False))
        text = int(ctx.setting("text_max_characters", 0))
        timeout = float(ctx.setting("timeout_seconds", 30.0))
        ctx.register_searcher(
            "exa",
            lambda: Exa(
                workspace=workspace,
                highlights=highlights,
                summary=summary,
                text_max_characters=text,
                timeout_seconds=timeout,
                **settings,
            ),
        )
