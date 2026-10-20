"""Tavily, through its Search API: a web_search backend built for agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ultron.sdk.auth import SecretRef, read_dotenv, resolve
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import CredentialError
from ultron.sdk.web import Answer, Found, Query, Result, post

ENDPOINT = "https://api.tavily.com/search"
MAX_COUNT = 20
DEPTHS = ("basic", "advanced", "fast", "ultra-fast")
TOPICS = ("general", "news", "finance")
ANSWERS = ("", "basic", "advanced")


class Tavily:
    name = "tavily"
    priority = 50
    """Beside Brave: a free monthly allowance, then a per-search price."""

    def __init__(
        self,
        *,
        workspace: Path | str = ".",
        api_key_env: str = "TAVILY_API_KEY",
        search_depth: str = "basic",
        topic: str = "general",
        include_answer: str = "",
        country: str = "",
        language: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.ref = SecretRef("env", api_key_env or "TAVILY_API_KEY")
        depth = search_depth.strip().lower()
        self.search_depth = depth if depth in DEPTHS else "basic"
        kind = topic.strip().lower()
        self.topic = kind if kind in TOPICS else "general"
        answer = include_answer.strip().lower()
        self.include_answer = answer if answer in ANSWERS else ""
        self.country = country.strip().lower()
        self.language = language.strip().lower()
        self.timeout = max(1.0, float(timeout_seconds))

    def ready(self) -> bool:
        """Whether a key can be had. Read fresh, never cached at install."""
        try:
            return bool(self._key())
        except CredentialError:
            return False

    def _key(self) -> str:
        return resolve(self.ref, dotenv=read_dotenv(self.workspace))

    def body(self, query: Query) -> dict[str, Any]:
        """The request Tavily is sent for this query, without the key."""
        body: dict[str, Any] = {
            "query": query.text,
            "max_results": max(1, min(query.count, MAX_COUNT)),
            "search_depth": self.search_depth,
            "topic": self.topic,
            # Off by default in Tavily; asked for so a row can carry its date.
            "include_published_date": True,
        }
        if self.include_answer:
            body["include_answer"] = self.include_answer
        if query.after or query.before:
            # Tavily takes either end on its own, so nothing is invented.
            if query.after:
                body["start_date"] = query.after
            if query.before:
                body["end_date"] = query.before
        elif query.freshness:
            body["time_range"] = query.freshness
        domains = query.domains or ((query.site,) if query.site else ())
        if domains:
            body["include_domains"] = list(domains)
        if query.excluded:
            body["exclude_domains"] = list(query.excluded)
        # Tavily boosts a country only on the general topic, and names it in
        # words ("germany"), so it is the operator's setting rather than the
        # two-letter code a Query carries.
        if self.country and self.topic == "general":
            body["country"] = self.country
        language = self.language or query.language.lower()
        if language:
            body["language"] = language
        return body

    async def search(self, query: Query) -> Found:
        key = self._key()
        response = await post(
            ENDPOINT,
            json=self.body(query),
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
            max_bytes=2_000_000,
            timeout=self.timeout,
        )
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} from Tavily: {_why(response.body)}")
        try:
            payload = json.loads(response.body)
        except ValueError as error:
            raise RuntimeError(f"Tavily returned something that is not JSON: {error}") from None
        answer = payload.get("answer") if isinstance(payload, dict) else None
        return Found(
            results=tuple(_rows(payload)),
            answer=Answer(text=answer) if isinstance(answer, str) and answer.strip() else None,
        )


def _rows(payload: Any) -> list[Result]:
    rows: list[Result] = []
    items = payload.get("results", []) if isinstance(payload, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            Result(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "") or ""),
                published=str(item.get("published_date", "") or "")[:10],
            )
        )
    return rows


def _why(body: bytes) -> str:
    """Tavily's error text, if it sent one, without printing a page.

    Most errors are `{"detail": {"error": ...}}`; a validation error is
    `{"detail": [{"loc": ..., "msg": ...}]}`."""
    try:
        data = json.loads(body)
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            return str(detail.get("error") or detail.get("message") or "")
        if isinstance(detail, list) and detail and isinstance(detail[0], dict):
            first = detail[0]
            where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
            return f"{where}: {first.get('msg', '')}" if where else str(first.get("msg", ""))
        if isinstance(detail, str):
            return detail
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class TavilyPlugin(Plugin):
    name = "tavily"
    description = "Tavily as a web_search backend."

    def register(self, ctx: PluginContext) -> None:
        workspace = ctx.workspace
        settings = {
            key: str(ctx.setting(key, default))
            for key, default in (
                ("api_key_env", "TAVILY_API_KEY"),
                ("search_depth", "basic"),
                ("topic", "general"),
                ("include_answer", ""),
                ("country", ""),
                ("language", ""),
            )
        }
        timeout = float(ctx.setting("timeout_seconds", 30.0))
        ctx.register_searcher(
            "tavily",
            lambda: Tavily(workspace=workspace, timeout_seconds=timeout, **settings),
        )
