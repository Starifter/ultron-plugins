"""SearXNG, a meta-search engine you run yourself, as a web_search backend."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from ultron.sdk.auth import SecretRef, read_dotenv, resolve
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.runtime import CredentialError, NetworkPolicyError
from ultron.sdk.web import Found, Query, Result, check_address, check_url, get, resolve_endpoint

BASE_URL_ENV = "SEARXNG_BASE_URL"
RESPONSE_BYTES = 1_000_000
"""OpenClaw's cap on a SearXNG reply."""
BUCKETS = ((1, "day"), (7, "week"), (31, "month"), (366, "year"))
"""SearXNG's time ranges, by how many days each reaches back."""


def search_url(base_url: str, q: str, params: dict[str, str]) -> str:
    """OpenClaw's URL: the base, `/search` unless the path already ends there,
    and only the parameters asked for - any query the base carried is dropped."""
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    path = path if path.endswith("/search") else f"{path}/search"
    query = urlencode({"q": q, "format": "json", **params})
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def time_range(query: Query, today: date) -> str:
    """SearXNG has buckets, not a range. A start date becomes the smallest
    bucket that reaches back to it; an end date cannot be said, and a start more
    than a year back is no narrower than no filter at all."""
    if query.after or query.before:
        if query.before or not query.after:
            return ""
        days = (today - date.fromisoformat(query.after)).days
        return next((name for reach, name in BUCKETS if days <= reach), "")
    return query.freshness


def is_private(address: str) -> bool:
    """Whether the policy would refuse this address without `allow_private` -
    loopback, a private range, link-local - which is what "your own network" is."""
    try:
        check_address(address)
    except NetworkPolicyError:
        return True
    return False


class SearXNG:
    name = "searxng"
    priority = 40
    """Before the searchers that cost money: an instance you run costs nothing
    per search."""

    def __init__(
        self,
        *,
        workspace: Path | str = ".",
        base_url: str = "",
        categories: str = "",
        language: str = "",
        timeout_seconds: float = 20.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.configured = base_url.strip()
        self.categories = ",".join(c.strip() for c in categories.split(",") if c.strip())
        self.language = language.strip()
        self.timeout = max(1.0, float(timeout_seconds))

    def base_url(self) -> str:
        """The setting, else `SEARXNG_BASE_URL`. Read fresh, never cached at install."""
        if self.configured:
            return self.configured.rstrip("/")
        try:
            found = resolve(SecretRef("env", BASE_URL_ENV), dotenv=read_dotenv(self.workspace))
        except CredentialError:
            return ""
        return found.strip().rstrip("/")

    def ready(self) -> bool:
        return bool(self.base_url())

    async def where(self) -> tuple[str, bool]:
        """The base URL, checked, and whether it is on this machine or your network.

        OpenClaw's rule. `http://` only to an instance every address of which is
        private or loopback: a query in clear text across the internet is one
        anybody on the path reads. `https://` anywhere, and the policy the core
        applies to every fetch still holds for a public one."""
        base = self.base_url()
        if not base:
            raise RuntimeError(
                f"no SearXNG instance: set {BASE_URL_ENV} or plugins_settings.searxng.base_url"
            )
        try:
            checked = check_url(base, allow_private=True)
            endpoint = await resolve_endpoint(checked, allow_private=True)
        except NetworkPolicyError as error:
            raise RuntimeError(f"SearXNG's base URL is not usable: {error}") from None
        private = all(is_private(address) for address in endpoint.addresses)
        if checked.startswith("http://") and not private:
            raise RuntimeError(
                "SearXNG over http:// must be on this machine or your network; "
                "a public instance needs https://"
            )
        return checked, private

    async def search(self, query: Query) -> Found:
        base, private = await self.where()
        q = self._q(query)
        params: dict[str, str] = {}
        language = self.language or query.language
        if language:
            params["language"] = language
        span = time_range(query, datetime.now(UTC).date())
        if span:
            params["time_range"] = span
        count = max(1, query.count)

        rows = await self._fetch(base, private, q, params, self.categories, count)
        if not rows and self.categories and "general" not in self.categories.lower().split(","):
            # OpenClaw's retry: a narrow category that found nothing is asked
            # again as a general search rather than reported empty.
            rows = await self._fetch(base, private, q, params, "general", count)
        return Found(results=tuple(rows))

    async def _fetch(
        self,
        base: str,
        private: bool,
        q: str,
        params: dict[str, str],
        categories: str,
        count: int,
    ) -> list[Result]:
        asked = {**params, **({"categories": categories} if categories else {})}
        response = await get(
            search_url(base, q, asked),
            headers={"Accept": "application/json"},
            allow_private=private,
            max_bytes=RESPONSE_BYTES,
            timeout=self.timeout,
        )
        if response.status == 403:
            raise RuntimeError(
                "HTTP 403 from SearXNG: JSON is probably not enabled - add json to "
                "search.formats in its settings.yml"
            )
        if response.status != 200:
            said = response.body[:200].decode("utf-8", "replace").strip()
            raise RuntimeError(f"HTTP {response.status} from SearXNG: {said}")
        if response.truncated:
            raise RuntimeError(f"SearXNG's reply was larger than {RESPONSE_BYTES} bytes")
        try:
            payload = json.loads(response.body)
        except ValueError:
            raise RuntimeError("SearXNG returned something that is not JSON") from None
        return _rows(payload)[:count]

    def _q(self, query: Query) -> str:
        """Domains as `site:` operators, which SearXNG hands to its engines and
        most of them read."""
        operators = [f"site:{d}" for d in query.domains] + [f"-site:{d}" for d in query.excluded]
        if not operators and query.site:
            operators = [f"site:{query.site}"]
        return " ".join([*operators, query.text]).strip()


def _rows(payload: Any) -> list[Result]:
    """Each result with a string `url` and `title`, as OpenClaw keeps them."""
    rows: list[Result] = []
    items = payload.get("results", []) if isinstance(payload, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url, title = item.get("url"), item.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            continue
        content = item.get("content")
        published = item.get("publishedDate")
        rows.append(
            Result(
                title=title,
                url=url,
                snippet=content if isinstance(content, str) else "",
                published=published[:10] if isinstance(published, str) else "",
            )
        )
    return rows


class SearXNGPlugin(Plugin):
    name = "searxng"
    description = "SearXNG as a web_search backend."

    def register(self, ctx: PluginContext) -> None:
        workspace = ctx.workspace
        settings = {
            key: str(ctx.setting(key, "")) for key in ("base_url", "categories", "language")
        }
        timeout = float(ctx.setting("timeout_seconds", 20.0))
        ctx.register_searcher(
            "searxng",
            lambda: SearXNG(workspace=workspace, timeout_seconds=timeout, **settings),
        )
