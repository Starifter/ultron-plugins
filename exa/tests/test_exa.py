"""The Exa searcher, driven with `post` replaced: nothing here touches a socket.

Run from a checkout of Ultron (`uv run pytest path/to/exa/tests`), which
supplies pytest-asyncio in auto mode and the `ultron.sdk` the plugin imports.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ultron.sdk.runtime import NetworkPolicyError
from ultron.sdk.web import Query, Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("ultron_plugin_exa", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()

PAYLOAD = {
    "requestId": "r1",
    "results": [
        {
            "title": "One",
            "url": "https://one.tld/a",
            "publishedDate": "2026-01-14T00:00:00.000Z",
            "highlights": ["First passage.", "  ", "Second passage."],
            "summary": "A summary.",
        },
        {"title": "Two", "url": "https://two.tld/b", "summary": "Only a summary."},
        {"title": "Three", "url": "https://three.tld/c", "text": "Only text."},
        "not a row",
    ],
}


def response(body: Any, status: int = 200, truncated: bool = False) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(
        url="https://api.exa.ai/search",
        status=status,
        headers=(("content-type", "application/json"),),
        body=raw,
        truncated=truncated,
    )


class Canned:
    """A `post` that answers one response and records what it was asked."""

    def __init__(self, answer: Response) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Response:
        self.calls.append({"url": url, **kwargs})
        return self.answer


def keyed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> Any:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    env = tmp_path / ".ultron"
    env.mkdir(exist_ok=True)
    (env / ".env").write_text("EXA_API_KEY=exa-secret-123\n", encoding="utf-8")
    return plugin.Exa(workspace=tmp_path, **settings)


def test_ready_only_with_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert plugin.Exa(workspace=tmp_path).ready() is False

    assert keyed(tmp_path, monkeypatch).ready() is True

    monkeypatch.setenv("EXA_API_KEY", "from-env")
    assert plugin.Exa(workspace=tmp_path / "elsewhere").ready() is True


@pytest.mark.parametrize(
    ("base", "endpoint"),
    [
        ("", "https://api.exa.ai/search"),
        ("https://api.exa.ai", "https://api.exa.ai/search"),
        ("https://api.exa.ai/", "https://api.exa.ai/search"),
        ("proxy.example.com/exa", "https://proxy.example.com/exa/search"),
        ("https://proxy.example.com/exa/search/", "https://proxy.example.com/exa/search"),
        ("https://proxy.example.com/x?drop=me", "https://proxy.example.com/x/search"),
    ],
)
def test_the_endpoint_is_normalised_as_openclaw_does(base: str, endpoint: str) -> None:
    assert plugin.endpoint_of(base) == endpoint


@pytest.mark.parametrize(
    "base", ["ftp://api.exa.ai", "https://user:pass@proxy.example.com", "http://127.0.0.1:9"]
)
def test_a_base_url_that_is_not_usable_is_refused(base: str) -> None:
    with pytest.raises(NetworkPolicyError):
        plugin.endpoint_of(base)


@pytest.mark.parametrize(
    ("freshness", "today", "start"),
    [
        ("day", date(2026, 3, 1), "2026-02-28"),
        ("week", date(2026, 3, 1), "2026-02-22"),
        ("month", date(2026, 3, 31), "2026-02-28"),
        ("month", date(2026, 1, 15), "2025-12-15"),
        ("year", date(2028, 2, 29), "2027-02-28"),
        ("year", date(2026, 10, 20), "2025-10-20"),
    ],
)
def test_freshness_becomes_a_start_date(freshness: str, today: date, start: str) -> None:
    assert plugin.freshness_start(freshness, today) == start


async def test_maps_the_query_and_reads_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "post", canned)

    found = await keyed(tmp_path, monkeypatch, country="us").search(
        Query(
            text="free threading",
            count=500,
            domains=("docs.python.org",),
            excluded=("pinterest.com",),
            after="2026-01-01",
            before="2026-06-30",
        )
    )

    sent = canned.calls[0]
    assert sent["url"] == "https://api.exa.ai/search"
    assert sent["headers"]["x-api-key"] == "exa-secret-123"
    assert sent["headers"]["x-exa-integration"] == "ultron"
    assert sent["json"] == {
        "query": "free threading",
        "numResults": 100,
        "type": "auto",
        "contents": {"highlights": True},
        "includeDomains": ["docs.python.org"],
        "excludeDomains": ["pinterest.com"],
        "startPublishedDate": "2026-01-01",
        "endPublishedDate": "2026-06-30",
        "userLocation": "US",
    }
    assert "exa-secret-123" not in json.dumps(sent["json"])

    assert [row.title for row in found.results] == ["One", "Two", "Three"]
    assert found.results[0].snippet == "First passage. ... Second passage."
    assert found.results[0].published == "2026-01-14"
    assert found.results[1].snippet == "Only a summary."
    assert found.results[2].snippet == "Only text."
    assert found.answer is None


async def test_freshness_is_sent_as_a_start_date_and_dates_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    searcher = keyed(tmp_path, monkeypatch)

    body = searcher.body(Query(text="x", freshness="week"), today=date(2026, 10, 20))
    assert body["startPublishedDate"] == "2026-10-13"

    body = searcher.body(Query(text="x", freshness="week", after="2026-01-01"))
    assert body["startPublishedDate"] == "2026-01-01"


async def test_the_count_is_held_even_when_exa_returns_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(PAYLOAD)))

    found = await keyed(tmp_path, monkeypatch).search(Query(text="x", count=2))

    assert len(found.results) == 2


def test_contents_follow_the_settings(tmp_path: Path) -> None:
    assert plugin.Exa(workspace=tmp_path).contents() == {"highlights": True}
    assert plugin.Exa(workspace=tmp_path, highlights=False, summary=True).contents() == {
        "summary": {}
    }
    assert plugin.Exa(workspace=tmp_path, text_max_characters=2000).contents() == {
        "highlights": True,
        "text": {"maxCharacters": 2000},
    }
    # Asking for nothing would leave every row empty, so it is OpenClaw's default.
    assert plugin.Exa(workspace=tmp_path, highlights=False).contents() == {"highlights": True}


def test_settings_that_are_not_words_fall_back(tmp_path: Path) -> None:
    assert plugin.Exa(workspace=tmp_path, search_type="Deep-Lite").search_type == "deep-lite"
    assert plugin.Exa(workspace=tmp_path, search_type="deep; drop").search_type == "auto"


async def test_a_deep_search_writes_an_answer_cited_by_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {**PAYLOAD, "output": {"content": "Yes, since 3.14.", "grounding": []}}
    canned = Canned(response(payload))
    monkeypatch.setattr(plugin, "post", canned)

    found = await keyed(tmp_path, monkeypatch, search_type="deep").search(Query(text="x"))

    assert canned.calls[0]["json"]["type"] == "deep"
    assert found.answer is not None
    assert found.answer.text == "Yes, since 3.14."
    assert found.answer.citations[0] == "https://one.tld/a"


async def test_an_object_output_is_not_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {**PAYLOAD, "output": {"content": {"field": "value"}}}
    monkeypatch.setattr(plugin, "post", Canned(response(payload)))

    found = await keyed(tmp_path, monkeypatch).search(Query(text="x"))

    assert found.answer is None


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (401, {"requestId": "r", "error": "Invalid API key", "tag": "INVALID_API_KEY"}, "Invalid"),
        (402, {"requestId": "r", "error": "Out of credits"}, "Out of credits"),
        (503, b"<html>unavailable</html>", "unavailable"),
    ],
)
async def test_an_error_status_becomes_a_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, body: Any, said: str
) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(body, status=status)))

    with pytest.raises(RuntimeError, match=f"HTTP {status} from Exa: .*{said}"):
        await keyed(tmp_path, monkeypatch).search(Query(text="x"))


async def test_a_truncated_or_unreadable_reply_is_said_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(PAYLOAD, truncated=True)))
    with pytest.raises(RuntimeError, match="larger than"):
        await keyed(tmp_path, monkeypatch).search(Query(text="x"))

    monkeypatch.setattr(plugin, "post", Canned(response(b"not json")))
    with pytest.raises(RuntimeError, match="not JSON"):
        await keyed(tmp_path, monkeypatch).search(Query(text="x"))


async def test_a_bad_base_url_fails_the_search_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "post", canned)

    searcher = keyed(tmp_path, monkeypatch, base_url="https://user:pw@proxy.example.com")
    with pytest.raises(RuntimeError, match="base_url is not usable"):
        await searcher.search(Query(text="x"))
    assert canned.calls == []


def test_register_installs_one_searcher_with_its_settings(tmp_path: Path) -> None:
    registered: dict[str, Any] = {}

    class Ctx:
        workspace = tmp_path

        def setting(self, key: str, default: Any) -> Any:
            return {"search_type": "lite", "summary": True, "timeout_seconds": 12}.get(key, default)

        def register_searcher(self, name: str, factory: Any) -> None:
            registered[name] = factory

    plugin.ExaPlugin().register(Ctx())  # type: ignore[arg-type]

    built = registered["exa"]()
    assert built.name == "exa"
    assert built.search_type == "lite"
    assert built.summary is True
    assert built.timeout == 12
    assert built.priority == 50
