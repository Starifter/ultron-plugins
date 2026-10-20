"""Perplexity as a web_search backend: its Search API, or a written answer from its Agent API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ultron.sdk.auth import SecretRef, read_dotenv, resolve
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import CredentialError
from ultron.sdk.web import Answer, Found, Query, Result, post

SEARCH_ENDPOINT = "https://api.perplexity.ai/search"
AGENT_ENDPOINT = "https://api.perplexity.ai/v1/agent"
MAX_COUNT = 20
MAX_DOMAINS = 20
MODES = ("search", "agent")
SEARCH_TYPES = ("web", "fast")


def us_date(iso: str) -> str:
    """`YYYY-MM-DD` as the `MM/DD/YYYY` Perplexity's date filters take."""
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


class Perplexity:
    name = "perplexity"

    def __init__(
        self,
        *,
        workspace: Path | str = ".",
        api_key_env: str = "PERPLEXITY_API_KEY",
        mode: str = "search",
        search_type: str = "web",
        preset: str = "fast",
        country: str = "",
        language: str = "",
        timeout_seconds: float = 0.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.ref = SecretRef("env", api_key_env or "PERPLEXITY_API_KEY")
        chosen = mode.strip().lower()
        self.mode = chosen if chosen in MODES else "search"
        kind = search_type.strip().lower()
        self.search_type = kind if kind in SEARCH_TYPES else "web"
        self.preset = preset.strip() or "fast"
        self.country = country.strip().upper()
        self.language = language.strip().lower()
        # An agent run searches and then writes, so it is given longer.
        default = 90.0 if self.mode == "agent" else 30.0
        self.timeout = max(1.0, float(timeout_seconds)) if timeout_seconds else default

    @property
    def priority(self) -> int:
        """Beside Brave for a search; after it for an answer, which is billed
        by the token on top of the search."""
        return 70 if self.mode == "agent" else 50

    def ready(self) -> bool:
        """Whether a key can be had. Read fresh, never cached at install."""
        try:
            return bool(self._key())
        except CredentialError:
            return False

    def _key(self) -> str:
        return resolve(self.ref, dotenv=read_dotenv(self.workspace))

    def filters(self, query: Query) -> dict[str, Any]:
        """The filters both endpoints share, under the names both use."""
        filters: dict[str, Any] = {}
        # One list, an allow list or a deny list and never both. A search
        # asked for both keeps the allow list: the excluded hosts are mostly
        # outside it anyway, and a stray one is visible in the row.
        domains = query.domains or ((query.site,) if query.site else ())
        if domains:
            filters["search_domain_filter"] = list(domains[:MAX_DOMAINS])
        elif query.excluded:
            filters["search_domain_filter"] = [f"-{d}" for d in query.excluded[:MAX_DOMAINS]]
        if query.after or query.before:
            if query.after:
                filters["search_after_date_filter"] = us_date(query.after)
            if query.before:
                filters["search_before_date_filter"] = us_date(query.before)
        elif query.freshness:
            filters["search_recency_filter"] = query.freshness
        return filters

    def search_body(self, query: Query) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query.text,
            "max_results": max(1, min(query.count, MAX_COUNT)),
            "search_type": self.search_type,
            **self.filters(query),
        }
        country = self.country or query.country.upper()
        if len(country) == 2:
            body["country"] = country
        language = self.language or query.language.lower()
        if len(language) == 2:
            body["search_language_filter"] = [language]
        return body

    def agent_body(self, query: Query) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": "web_search",
            "search_type": self.search_type,
            "max_results": max(1, min(query.count, MAX_COUNT)),
        }
        filters = self.filters(query)
        if filters:
            tool["filters"] = filters
        country = self.country or query.country.upper()
        if len(country) == 2:
            tool["user_location"] = {"country": country}
        body: dict[str, Any] = {
            "preset": self.preset,
            "input": query.text,
            "tools": [tool],
            # Nothing Ultron asks is kept at Perplexity for later retrieval.
            "store": False,
        }
        language = self.language or query.language.lower()
        if len(language) == 2:
            body["language_preference"] = language
        return body

    async def search(self, query: Query) -> Found:
        key = self._key()
        agent = self.mode == "agent"
        response = await post(
            AGENT_ENDPOINT if agent else SEARCH_ENDPOINT,
            json=self.agent_body(query) if agent else self.search_body(query),
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
            max_bytes=4_000_000,
            timeout=self.timeout,
        )
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} from Perplexity: {_why(response.body)}")
        try:
            payload = json.loads(response.body)
        except ValueError as error:
            raise RuntimeError(f"Perplexity returned something that is not JSON: {error}") from None
        if not isinstance(payload, dict):
            raise RuntimeError("Perplexity returned something that is not an object")
        return _agent_found(payload) if agent else Found(results=tuple(_rows(payload)))


def _rows(payload: dict[str, Any]) -> list[Result]:
    rows: list[Result] = []
    items = payload.get("results", [])
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            Result(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("snippet", "") or ""),
                published=str(item.get("date", "") or "")[:10],
            )
        )
    return rows


def _agent_found(payload: dict[str, Any]) -> Found:
    """An Agent API response as an answer and the pages it searched.

    The answer is every `output_text` part of every `message` item; its
    citations are the `url_citation` annotations in the order they appear, and
    the pages the search returned when the answer carried none."""
    status = payload.get("status")
    if status != "completed":
        error = payload.get("error")
        why = error.get("message", "") if isinstance(error, dict) else ""
        raise RuntimeError(
            f"Perplexity's answer is {status or 'missing'}" + (f": {why}" if why else "")
        )
    texts: list[str] = []
    cited: list[str] = []
    rows: list[Result] = []
    output = payload.get("output", [])
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            parts = item.get("content", [])
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                texts.append(str(part.get("text", "") or ""))
                notes = part.get("annotations", [])
                for note in notes if isinstance(notes, list) else []:
                    if isinstance(note, dict) and note.get("type") == "url_citation":
                        url = str(note.get("url", "") or "")
                        if url and url not in cited:
                            cited.append(url)
        elif item.get("type") == "search_results":
            rows.extend(_rows(item))
    text = "\n".join(t for t in texts if t.strip()).strip()
    if not cited:
        cited = list(dict.fromkeys(row.url for row in rows if row.url))
    return Found(
        results=tuple(rows),
        answer=Answer(text=text, citations=tuple(cited)) if text else None,
    )


def _why(body: bytes) -> str:
    """Perplexity's error text, if it sent one, without printing a page.

    The Agent API says `{"error": {"message": ...}}`; a validation error is
    `{"detail": [{"loc": ..., "msg": ...}]}`."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error.get("type") or error.get("code") or "")
            if isinstance(error, str):
                return error
            detail = data.get("detail")
            if isinstance(detail, list) and detail and isinstance(detail[0], dict):
                first = detail[0]
                where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
                return f"{where}: {first.get('msg', '')}" if where else str(first.get("msg", ""))
            if isinstance(detail, str):
                return detail
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class PerplexityPlugin(Plugin):
    name = "perplexity"
    description = "Perplexity as a web_search backend."

    def register(self, ctx: PluginContext) -> None:
        workspace = ctx.workspace
        settings = {
            key: str(ctx.setting(key, default))
            for key, default in (
                ("api_key_env", "PERPLEXITY_API_KEY"),
                ("mode", "search"),
                ("search_type", "web"),
                ("preset", "fast"),
                ("country", ""),
                ("language", ""),
            )
        }
        timeout = float(ctx.setting("timeout_seconds", 0.0))
        ctx.register_searcher(
            "perplexity",
            lambda: Perplexity(workspace=workspace, timeout_seconds=timeout, **settings),
        )
