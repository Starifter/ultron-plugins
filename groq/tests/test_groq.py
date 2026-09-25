"""The Groq plugin, driven the way Ultron drives it: a fake `openai` client that
records each request and answers the way Groq's API documents it.

Run from a checkout of Ultron (`uv run pytest path/to/groq/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ultron.sdk.provider import ContextOverflowError, Message

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("ultron_plugin_groq", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
GroqProvider = plugin.GroqProvider


class FakeClient:
    def __init__(
        self, message: Any = None, *, models: list[Any] = (), fail: Exception | None = None
    ) -> None:
        self.message = message
        self.models_data = list(models)
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)], usage=None)

    async def _list(self) -> Any:
        for row in self.models_data:
            yield row


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


async def test_the_reply_ceiling_is_max_completion_tokens() -> None:
    client = FakeClient(reply(content="hi"))
    await GroqProvider("llama-3.3-70b-versatile", client=client).complete(
        system="", messages=[Message.user("hi")]
    )
    assert "max_completion_tokens" in client.request
    assert "max_tokens" not in client.request


async def test_a_model_without_a_thinking_control_is_sent_none() -> None:
    """Llama has no menu, so the session's default `high` sends nothing."""
    client = FakeClient(reply(content="hi"))
    provider = GroqProvider("llama-3.3-70b-versatile", client=client)
    assert provider.thinking_levels == ()
    await provider.complete(system="", messages=[Message.user("hi")])
    assert "reasoning_effort" not in client.request
    assert "reasoning_format" not in client.request.get("extra_body", {})


async def test_gpt_oss_takes_an_effort_and_has_no_off() -> None:
    assert GroqProvider.levels_for("openai/gpt-oss-120b") == ("low", "medium", "high")
    client = FakeClient(reply(content="hi", reasoning="thought about it"))
    provider = GroqProvider("openai/gpt-oss-120b", client=client, thinking="medium")
    turn = await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "medium"
    assert "reasoning_format" not in client.request.get("extra_body", {})
    assert turn.thinking == "thought about it" and turn.text == "hi"


async def test_qwen_turns_off_with_none_and_is_parsed() -> None:
    client = FakeClient(reply(content="hi"))
    await GroqProvider("qwen/qwen3.8-27b", client=client, thinking="off").complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["reasoning_effort"] == "none"
    assert client.request["extra_body"]["reasoning_format"] == "parsed"


async def test_the_listing_keeps_window_ceiling_and_menu_and_drops_inactive() -> None:
    rows = [
        {
            "id": "openai/gpt-oss-120b",
            "created": 1754400000,
            "context_window": 131072,
            "max_completion_tokens": 65536,
            "active": True,
        },
        {"id": "retired-model", "context_window": 8192, "active": False},
    ]
    entries = await GroqProvider(client=FakeClient(models=rows)).list_models()
    assert entries is not None and [e.id for e in entries] == ["openai/gpt-oss-120b"]
    (entry,) = entries
    assert (entry.context_window, entry.max_output) == (131072, 65536)
    assert entry.thinking_levels == ("low", "medium", "high")
    assert entry.released == "2025-08-05"


async def test_groqs_overflow_is_a_context_overflow() -> None:
    class BadRequest(Exception):
        status_code = 400

    error = BadRequest(
        "Error code: 400 - {'error': {'message': 'Please reduce the length of the messages or "
        "completion.', 'code': 'context_length_exceeded'}}"
    )
    provider = GroqProvider("llama-3.3-70b-versatile", client=FakeClient(fail=error))
    try:
        await provider.complete(system="", messages=[Message.user("hi")])
    except ContextOverflowError as exc:
        assert exc.__cause__ is error
    else:
        raise AssertionError("an overflow was not recognised")


def test_the_plugin_registers_the_provider() -> None:
    registered: dict[str, Any] = {}
    ctx = SimpleNamespace(
        register_provider=lambda name, cls: registered.__setitem__(name, cls),
        setting=lambda key, default=None: default,
    )
    plugin.GroqPlugin().register(ctx)  # type: ignore[arg-type]
    assert registered == {"groq": GroqProvider}
