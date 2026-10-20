"""The Perplexity searcher, driven with `post` replaced: nothing here touches a socket.

Run from a checkout of Ultron (`uv run pytest path/to/perplexity/tests`), which
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
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_perplexity", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()

SEARCH_PAYLOAD = {
    "id": "e38104d5",
    "results": [
        {
            "title": "One",
            "url": "https://one.tld/a",
            "snippet": "A snippet.",
            "date": "2026-01-23",
            "last_updated": "2026-09-25",
        },
        {"title": "Two", "url": "https://two.tld/b", "snippet": "Another.", "date": None},
    ],
}

AGENT_PAYLOAD = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "preset": "fast@3",
    "output": [
        {
            "type": "search_results",
            "queries": ["free threading"],
            "results": [
                {
                    "id": 1,
                    "url": "https://one.tld/a",
                    "title": "One",
                    "snippet": "A.",
                    "date": "2026-01-23",
                },
                {"id": 2, "url": "https://two.tld/b", "title": "Two", "snippet": "B."},
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "It is supported since 3.14 [1][2].",
                    "annotations": [
                        {"type": "url_citation", "url": "https://two.tld/b", "title": "Two"},
                        {"type": "url_citation", "url": "https://one.tld/a", "title": "One"},
                        {"type": "url_citation", "url": "https://two.tld/b", "title": "Two"},
                    ],
                }
            ],
        },
    ],
}


def response(body: Any, status: int = 200) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(
        url=plugin.SEARCH_ENDPOINT,
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
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    env = tmp_path / ".ultron"
    env.mkdir(exist_ok=True)
    (env / ".env").write_text("PERPLEXITY_API_KEY=pplx-secret-123\n", encoding="utf-8")
    return plugin.Perplexity(workspace=tmp_path, **settings)


def test_ready_only_with_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    assert plugin.Perplexity(workspace=tmp_path).ready() is False

    assert keyed(tmp_path, monkeypatch).ready() is True

    monkeypatch.setenv("PERPLEXITY_API_KEY", "from-env")
    assert plugin.Perplexity(workspace=tmp_path / "elsewhere").ready() is True


def test_us_date() -> None:
    assert plugin.us_date("2026-03-07") == "03/07/2026"


# -- search mode ------------------------------------------------------------


async def test_search_maps_the_query_and_reads_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response(SEARCH_PAYLOAD))
    monkeypatch.setattr(plugin, "post", canned)

    found = await keyed(tmp_path, monkeypatch, country="us").search(
        Query(text="free threading", count=50, freshness="week", domains=("docs.python.org",))
    )

    sent = canned.calls[0]
    assert sent["url"] == "https://api.perplexity.ai/search"
    assert sent["headers"]["Authorization"] == "Bearer pplx-secret-123"
    assert sent["json"] == {
        "query": "free threading",
        "max_results": 20,
        "search_type": "web",
        "search_domain_filter": ["docs.python.org"],
        "search_recency_filter": "week",
        "country": "US",
    }
    assert sent["timeout"] == 30.0

    assert [row.title for row in found.results] == ["One", "Two"]
    assert found.results[0].published == "2026-01-23"
    assert found.results[1].published == ""
    assert found.answer is None


async def test_dates_go_as_perplexity_writes_them_and_win_over_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "post", canned)

    await keyed(tmp_path, monkeypatch).search(
        Query(text="x", freshness="day", after="2026-01-01", before="2026-06-30")
    )

    body = canned.calls[0]["json"]
    assert body["search_after_date_filter"] == "01/01/2026"
    assert body["search_before_date_filter"] == "06/30/2026"
    assert "search_recency_filter" not in body


async def test_the_domain_filter_is_an_allow_list_or_a_deny_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "post", canned)
    searcher = keyed(tmp_path, monkeypatch)

    await searcher.search(Query(text="x", excluded=("pinterest.com", "quora.com")))
    assert canned.calls[-1]["json"]["search_domain_filter"] == ["-pinterest.com", "-quora.com"]

    await searcher.search(Query(text="x", domains=("a.tld",), excluded=("b.tld",)))
    assert canned.calls[-1]["json"]["search_domain_filter"] == ["a.tld"]

    await searcher.search(Query(text="x", site="docs.python.org"))
    assert canned.calls[-1]["json"]["search_domain_filter"] == ["docs.python.org"]

    many = tuple(f"d{i}.tld" for i in range(25))
    await searcher.search(Query(text="x", domains=many))
    assert len(canned.calls[-1]["json"]["search_domain_filter"]) == 20


async def test_a_locale_that_is_not_two_letters_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response({"results": []}))
    monkeypatch.setattr(plugin, "post", canned)

    await keyed(tmp_path, monkeypatch, country="Germany", language="english").search(
        Query(text="x")
    )
    body = canned.calls[0]["json"]
    assert "country" not in body
    assert "search_language_filter" not in body

    await keyed(tmp_path, monkeypatch, language="DE").search(Query(text="x"))
    assert canned.calls[1]["json"]["search_language_filter"] == ["de"]


# -- agent mode -------------------------------------------------------------


async def test_agent_mode_asks_the_agent_api_and_keeps_nothing_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = Canned(response(AGENT_PAYLOAD))
    monkeypatch.setattr(plugin, "post", canned)

    searcher = keyed(tmp_path, monkeypatch, mode="agent", country="de", language="de")
    await searcher.search(Query(text="free threading", count=8, excluded=("pinterest.com",)))

    sent = canned.calls[0]
    assert sent["url"] == "https://api.perplexity.ai/v1/agent"
    assert sent["headers"]["Authorization"] == "Bearer pplx-secret-123"
    assert sent["timeout"] == 90.0
    assert sent["json"] == {
        "preset": "fast",
        "input": "free threading",
        "tools": [
            {
                "type": "web_search",
                "search_type": "web",
                "max_results": 8,
                "filters": {"search_domain_filter": ["-pinterest.com"]},
                "user_location": {"country": "DE"},
            }
        ],
        "store": False,
        "language_preference": "de",
    }


async def test_agent_mode_returns_the_answer_its_citations_and_the_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(AGENT_PAYLOAD)))

    found = await keyed(tmp_path, monkeypatch, mode="agent").search(Query(text="x"))

    assert found.answer is not None
    assert found.answer.text == "It is supported since 3.14 [1][2]."
    assert found.answer.citations == ("https://two.tld/b", "https://one.tld/a")
    assert [row.url for row in found.results] == ["https://one.tld/a", "https://two.tld/b"]
    assert found.results[0].published == "2026-01-23"


async def test_an_answer_without_annotations_cites_the_pages_it_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(json.dumps(AGENT_PAYLOAD))
    payload["output"][1]["content"][0]["annotations"] = []
    monkeypatch.setattr(plugin, "post", Canned(response(payload)))

    found = await keyed(tmp_path, monkeypatch, mode="agent").search(Query(text="x"))

    assert found.answer is not None
    assert found.answer.citations == ("https://one.tld/a", "https://two.tld/b")


async def test_an_agent_run_that_did_not_complete_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = {
        "id": "r",
        "status": "failed",
        "output": [],
        "error": {"message": "search unavailable"},
    }
    monkeypatch.setattr(plugin, "post", Canned(response(failed)))

    with pytest.raises(RuntimeError, match="answer is failed: search unavailable"):
        await keyed(tmp_path, monkeypatch, mode="agent").search(Query(text="x"))


def test_the_agent_mode_sorts_after_the_search_mode(tmp_path: Path) -> None:
    assert plugin.Perplexity(workspace=tmp_path).priority == 50
    assert plugin.Perplexity(workspace=tmp_path, mode="agent").priority == 70
    assert plugin.Perplexity(workspace=tmp_path, mode="nonsense").mode == "search"


# -- failures -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (
            400,
            {"error": {"message": "max_results out of range", "type": "invalid_request"}},
            "out of range",
        ),
        (
            422,
            {"detail": [{"loc": ["body", "query"], "msg": "field required"}]},
            "query: field required",
        ),
        (401, {"error": "Invalid API key"}, "Invalid API key"),
        (502, b"<html>bad gateway</html>", "bad gateway"),
    ],
)
async def test_an_error_status_becomes_a_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, body: Any, said: str
) -> None:
    monkeypatch.setattr(plugin, "post", Canned(response(body, status=status)))

    with pytest.raises(RuntimeError, match=f"HTTP {status} from Perplexity: .*{said}"):
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
            return {"mode": "agent", "preset": "low", "timeout_seconds": 45}.get(key, default)

        def register_searcher(self, name: str, factory: Any) -> None:
            registered[name] = factory

    plugin.PerplexityPlugin().register(Ctx())  # type: ignore[arg-type]

    built = registered["perplexity"]()
    assert built.name == "perplexity"
    assert built.mode == "agent"
    assert built.preset == "low"
    assert built.timeout == 45
