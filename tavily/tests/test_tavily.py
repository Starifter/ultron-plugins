"""The Tavily searcher, driven with `post` replaced: nothing here touches a socket.

Run from a checkout of Ultron (`uv run pytest path/to/tavily/tests`), which
supplies pytest-asyncio in auto mode and the `ultron.sdk` the plugin imports.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ultron.sdk.web import Query, Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("ultron_plugin_tavily", HERE.parent / "plugin.py")
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
            "title": "One",
            "url": "https://one.tld/a",
            "content": "The passage that answers.",
            "score": 0.9,
            "published_date": "2026-01-14T08:00:00Z",
        },
        {"title": "Two", "url": "https://two.tld/b", "content": "Another.", "published_date": None},
        "not a row",
    ],
}


def response(body: Any, status: int = 200) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(
        url=plugin.ENDPOINT,
        status=status,
        headers=(("content-type", "application/json"),),
        body=raw,
        truncated=False,
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
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    env = tmp_path / ".ultron"
    env.mkdir(exist_ok=True)
    (env / ".env").write_text("TAVILY_API_KEY=tvly-secret-123\n", encoding="utf-8")
    return plugin.Tavily(workspace=tmp_path, **settings)


def test_ready_only_with_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert plugin.Tavily(workspace=tmp_path).ready() is False

    assert keyed(tmp_path, monkeypatch).ready() is True

    monkeypatch.setenv("TAVILY_API_KEY", "from-env")
    assert plugin.Tavily(workspace=tmp_path / "elsewhere").ready() is True


async def test_maps_the_query_and_reads_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response(PAYLOAD))
    monkeypatch.setattr(plugin, "post", canned)

    found = await keyed(tmp_path, monkeypatch).search(
        Query(
            text="free threading",
            count=50,
            freshness="week",
            domains=("docs.python.org",),
            excluded=("pinterest.com",),
        )
    )

    sent = canned.calls[0]
    assert sent["url"] == "https://api.tavily.com/search"
    assert sent["headers"]["Authorization"] == "Bearer tvly-secret-123"
    assert sent["json"] == {
        "query": "free threading",
        "max_results": 20,
        "search_depth": "basic",
        "topic": "general",
        "include_published_date": True,
        "time_range": "week",
        "include_domains": ["docs.python.org"],
        "exclude_domains": ["pinterest.com"],
    }
    assert "tvly-secret-123" not in json.dumps(sent["json"])

    assert [row.title for row in found.results] == ["One", "Two"]
    assert found.results[0].snippet == "The passage that answers."
    assert found.results[0].published == "2026-01-14"
    assert found.results[1].published == ""
    assert found.answer is None


async def test_a_date_range_wins_over_freshness_and_needs_no_invented_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "post", canned)
    searcher = keyed(tmp_path, monkeypatch)

    await searcher.search(Query(text="x", freshness="day", after="2026-01-01"))
    body = canned.calls[-1]["json"]
    assert body["start_date"] == "2026-01-01"
    assert "end_date" not in body
    assert "time_range" not in body


async def test_the_old_site_field_still_restricts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "post", canned)

    await keyed(tmp_path, monkeypatch).search(Query(text="x", site="docs.python.org"))

    assert canned.calls[0]["json"]["include_domains"] == ["docs.python.org"]


async def test_an_answer_only_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canned = Canned(response({**PAYLOAD, "answer": "Yes, since 3.14."}))
    monkeypatch.setattr(plugin, "post", canned)

    found = await keyed(tmp_path, monkeypatch, include_answer="basic").search(Query(text="x"))

    assert canned.calls[0]["json"]["include_answer"] == "basic"
    assert found.answer is not None
    assert found.answer.text == "Yes, since 3.14."
    assert len(found.results) == 2


async def test_settings_reach_the_request_and_bad_ones_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "post", canned)

    await keyed(
        tmp_path,
        monkeypatch,
        search_depth="advanced",
        country="Germany",
        language="DE",
        include_answer="sometimes",
    ).search(Query(text="x"))
    body = canned.calls[-1]["json"]
    assert body["search_depth"] == "advanced"
    assert body["country"] == "germany"
    assert body["language"] == "de"
    assert "include_answer" not in body

    await keyed(tmp_path, monkeypatch, topic="news", country="germany", search_depth="deep").search(
        Query(text="x")
    )
    body = canned.calls[-1]["json"]
    assert body["topic"] == "news"
    assert body["search_depth"] == "basic"
    assert "country" not in body  # Tavily applies it to the general topic only


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (
            401,
            {"detail": {"error": "Unauthorized: missing or invalid API key."}},
            "invalid API key",
        ),
        (
            422,
            {"detail": [{"loc": ["body", "max_results"], "msg": "too big"}]},
            "max_results: too big",
        ),
        (500, b"<html>oops</html>", "oops"),
    ],
)
async def test_an_error_status_becomes_a_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, body: Any, said: str
) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(body, status=status)))

    with pytest.raises(RuntimeError, match=f"HTTP {status} from Tavily: .*{said}"):
        await keyed(tmp_path, monkeypatch).search(Query(text="x"))


async def test_not_json_is_said_plainly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(b"not json")))

    with pytest.raises(RuntimeError, match="not JSON"):
        await keyed(tmp_path, monkeypatch).search(Query(text="x"))


def test_register_installs_one_searcher_with_its_settings(tmp_path: Path) -> None:
    registered: dict[str, Any] = {}

    class Ctx:
        workspace = tmp_path

        def setting(self, key: str, default: Any) -> Any:
            return {"topic": "news", "timeout_seconds": 12}.get(key, default)

        def register_searcher(self, name: str, factory: Any) -> None:
            registered[name] = factory

    plugin.TavilyPlugin().register(Ctx())  # type: ignore[arg-type]

    built = registered["tavily"]()
    assert built.name == "tavily"
    assert built.topic == "news"
    assert built.timeout == 12
    assert built.priority == 50
