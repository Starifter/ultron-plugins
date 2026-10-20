"""The SearXNG searcher, driven with `get` and name resolution replaced: nothing
here touches a socket.

Run from a checkout of Ultron (`uv run pytest path/to/searxng/tests`), which
supplies pytest-asyncio in auto mode and the `ultron.sdk` the plugin imports.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from ultron.sdk.web import Endpoint, Query, Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_searxng", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()

PAYLOAD = {
    "query": "free threading",
    "results": [
        {
            "url": "https://one.tld/a",
            "title": "One",
            "content": "A snippet.",
            "publishedDate": "2026-01-14T08:00:00",
        },
        {"url": "https://two.tld/b", "title": "Two"},
        {"url": "https://three.tld/c"},  # no title: dropped, as OpenClaw drops it
        {"url": 3, "title": "bad"},
        "not a row",
    ],
}


def response(body: Any, status: int = 200, truncated: bool = False) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(
        url="http://localhost:8080/search",
        status=status,
        headers=(("content-type", "application/json"),),
        body=raw,
        truncated=truncated,
    )


class Canned:
    """A `get` that answers from a list in turn and records what it was asked."""

    def __init__(self, *answers: Response) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Response:
        self.calls.append({"url": url, **kwargs})
        return self.answers[min(len(self.calls), len(self.answers)) - 1]

    def params(self, index: int = 0) -> dict[str, str]:
        query = parse_qs(urlsplit(self.calls[index]["url"]).query)
        return {key: values[0] for key, values in query.items()}


def resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    async def fake(url: str, **kwargs: Any) -> Endpoint:
        parts = urlsplit(url)
        return Endpoint(
            url=url, host=parts.hostname or "", port=parts.port or 80, addresses=addresses
        )

    monkeypatch.setattr(plugin, "resolve_endpoint", fake)


def instance(base: str = "http://localhost:8080", **settings: Any) -> Any:
    return plugin.SearXNG(base_url=base, **settings)


# -- where the instance is ----------------------------------------------------


def test_ready_from_the_setting_or_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    assert plugin.SearXNG(workspace=tmp_path).ready() is False

    assert plugin.SearXNG(workspace=tmp_path, base_url="http://localhost:8080").ready() is True

    env = tmp_path / ".ultron"
    env.mkdir()
    (env / ".env").write_text("SEARXNG_BASE_URL=http://box.lan:8888/\n", encoding="utf-8")
    assert plugin.SearXNG(workspace=tmp_path).base_url() == "http://box.lan:8888"

    # The setting wins over the variable.
    assert (
        plugin.SearXNG(workspace=tmp_path, base_url="https://s.tld/").base_url() == "https://s.tld"
    )


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("http://localhost:8080", "http://localhost:8080/search"),
        ("http://localhost:8080/", "http://localhost:8080/search"),
        ("https://s.tld/searxng", "https://s.tld/searxng/search"),
        ("https://s.tld/searxng/search/", "https://s.tld/searxng/search"),
    ],
)
def test_the_search_path_is_added_once(base: str, url: str) -> None:
    built = plugin.search_url(base, "q", {})
    assert built.split("?", 1)[0] == url
    assert parse_qs(urlsplit(built).query) == {"q": ["q"], "format": ["json"]}


async def test_http_to_a_private_instance_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "127.0.0.1", "::1")
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "get", canned)

    await instance().search(Query(text="x"))

    assert canned.calls[0]["allow_private"] is True


async def test_http_to_a_public_instance_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "192.168.1.5", "93.184.216.34")  # one public answer is enough
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "get", canned)

    with pytest.raises(RuntimeError, match="http:// must be on this machine or your network"):
        await instance("http://search.example.com").search(Query(text="x"))
    assert canned.calls == []


async def test_https_to_a_public_instance_is_held_to_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "93.184.216.34")
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "get", canned)

    await instance("https://search.example.com").search(Query(text="x"))

    assert canned.calls[0]["allow_private"] is False


@pytest.mark.parametrize(
    ("base", "said"),
    [
        ("", "no SearXNG instance"),
        ("ftp://localhost", "not usable"),
        ("http://user:pass@localhost:8080", "not usable"),
    ],
)
async def test_an_unusable_base_url_is_said_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str, said: str
) -> None:
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    resolves_to(monkeypatch, "127.0.0.1")
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "get", canned)

    with pytest.raises(RuntimeError, match=said):
        await plugin.SearXNG(workspace=tmp_path, base_url=base).search(Query(text="x"))
    assert canned.calls == []


# -- the search ---------------------------------------------------------------


async def test_maps_the_query_and_reads_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "get", canned)

    found = await instance(categories="general, science", language="de").search(
        Query(
            text="free threading",
            count=5,
            freshness="month",
            domains=("docs.python.org",),
            excluded=("pinterest.com",),
        )
    )

    assert canned.calls[0]["url"].startswith("http://localhost:8080/search?")
    assert canned.calls[0]["headers"] == {"Accept": "application/json"}
    assert canned.params() == {
        "q": "site:docs.python.org -site:pinterest.com free threading",
        "format": "json",
        "language": "de",
        "time_range": "month",
        "categories": "general,science",
    }

    assert [row.title for row in found.results] == ["One", "Two"]
    assert found.results[0].snippet == "A snippet."
    assert found.results[0].published == "2026-01-14"
    assert found.results[1].snippet == ""


async def test_the_count_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    monkeypatch.setattr(plugin, "get", Canned(response(PAYLOAD)))

    found = await instance().search(Query(text="x", count=1))

    assert len(found.results) == 1


async def test_an_empty_narrow_search_is_asked_again_as_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    canned = Canned(response({"results": []}), response(PAYLOAD))
    monkeypatch.setattr(plugin, "get", canned)

    found = await instance(categories="science").search(Query(text="x"))

    assert [canned.params(i)["categories"] for i in range(2)] == ["science", "general"]
    assert len(found.results) == 2


async def test_an_empty_general_search_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "get", canned)

    for categories in ("", "general,news"):
        canned.calls.clear()
        found = await instance(categories=categories).search(Query(text="x"))
        assert found.results == ()
        assert len(canned.calls) == 1


@pytest.mark.parametrize(
    ("query", "span"),
    [
        (Query(text="x", freshness="week"), "week"),
        (Query(text="x", after="2026-10-19"), "day"),
        (Query(text="x", after="2026-10-14"), "week"),
        (Query(text="x", after="2026-09-25"), "month"),
        (Query(text="x", after="2026-01-01"), "year"),
        (Query(text="x", after="2024-01-01"), ""),
        (Query(text="x", after="2026-10-01", before="2026-10-10"), ""),
        (Query(text="x", freshness="day", before="2026-10-10"), ""),
    ],
)
def test_dates_become_the_smallest_bucket_that_reaches_them(query: Query, span: str) -> None:
    assert plugin.time_range(query, date(2026, 10, 20)) == span


# -- failures -----------------------------------------------------------------


async def test_a_403_says_how_to_turn_on_json(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    monkeypatch.setattr(plugin, "get", Canned(response(b"Forbidden", status=403)))

    with pytest.raises(RuntimeError, match=r"search\.formats"):
        await instance().search(Query(text="x"))


async def test_other_failures_are_said_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "127.0.0.1")

    monkeypatch.setattr(plugin, "get", Canned(response(b"upstream down", status=502)))
    with pytest.raises(RuntimeError, match="HTTP 502 from SearXNG: upstream down"):
        await instance().search(Query(text="x"))

    monkeypatch.setattr(plugin, "get", Canned(response(PAYLOAD, truncated=True)))
    with pytest.raises(RuntimeError, match="larger than"):
        await instance().search(Query(text="x"))

    monkeypatch.setattr(plugin, "get", Canned(response(b"<html>a page</html>")))
    with pytest.raises(RuntimeError, match="not JSON"):
        await instance().search(Query(text="x"))


def test_register_installs_one_searcher_with_its_settings(tmp_path: Path) -> None:
    registered: dict[str, Any] = {}

    class Ctx:
        workspace = tmp_path

        def setting(self, key: str, default: Any) -> Any:
            return {"base_url": "http://localhost:8080", "timeout_seconds": 5}.get(key, default)

        def register_searcher(self, name: str, factory: Any) -> None:
            registered[name] = factory

    plugin.SearXNGPlugin().register(Ctx())  # type: ignore[arg-type]

    built = registered["searxng"]()
    assert built.name == "searxng"
    assert built.ready() is True
    assert built.timeout == 5
    assert built.priority == 40
