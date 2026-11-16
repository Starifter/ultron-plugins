"""The Venice plugin, driven the way Ultron drives it: a fake `openai` client that
records each request and answers the way Venice's API documents it.

Run from a checkout of Ultron (`uv run pytest path/to/venice/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ultron.sdk.provider import Message, PriceTier, ToolResultBlock
from ultron.sdk.runtime import ProviderError
from ultron.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("ultron_plugin_venice", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
VeniceProvider = plugin.VeniceProvider

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})


class FakeClient:
    def __init__(
        self,
        replies: list[Any] = (),
        *,
        usage: Any = None,
        models: list[Any] = (),
        fail: Exception | None = None,
    ) -> None:
        self.replies = list(replies)
        self.usage = usage
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
        message = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=self.usage)

    async def _list(self) -> Any:
        for row in self.models_data:
            yield row


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


def usd(rate: float) -> dict[str, float]:
    return {"usd": rate, "diem": rate}


CALL = {"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}

GLM = {
    "id": "zai-org-glm-5-1",
    "type": "text",
    "created": 1767225600,
    "context_length": 198000,
    "model_spec": {
        "availableContextTokens": 198000,
        "maxCompletionTokens": 64000,
        "pricing": {"input": usd(0.9), "cache_input": usd(0.18), "output": usd(3.0)},
        "capabilities": {
            "supportsFunctionCalling": True,
            "supportsVision": False,
            "supportsReasoning": True,
            "supportsReasoningEffort": True,
            "reasoningEffortOptions": ["none", "low", "medium", "high"],
            "defaultReasoningEffort": "low",
        },
        "offline": False,
    },
}


def model(model_id: str, **spec: Any) -> dict[str, Any]:
    capabilities = {"supportsFunctionCalling": True, **spec.pop("capabilities", {})}
    return {
        "id": model_id,
        "type": "text",
        "model_spec": {
            "availableContextTokens": 100000,
            "pricing": {"input": usd(1.0), "output": usd(2.0)},
            "capabilities": capabilities,
            **spec,
        },
    }


async def test_venices_own_prompt_is_off_and_the_ceiling_has_its_name() -> None:
    client = FakeClient([reply(content="hi")])
    await VeniceProvider("zai-org-glm-5-1", client=client).complete(
        system="be brief", messages=[Message.user("hi")]
    )
    assert client.request["extra_body"]["venice_parameters"] == {
        "include_venice_system_prompt": False
    }
    assert "max_completion_tokens" in client.request and "max_tokens" not in client.request


async def test_the_setting_turns_venices_prompt_back_on() -> None:
    client = FakeClient([reply(content="hi")])
    bound = VeniceProvider.configured({"venice_system_prompt": True})
    await bound("zai-org-glm-5-1", client=client).complete(system="", messages=[Message.user("hi")])
    assert client.request["extra_body"]["venice_parameters"] == {
        "include_venice_system_prompt": True
    }


async def test_thinking_is_an_effort_from_the_models_own_menu() -> None:
    client = FakeClient([reply(content="a"), reply(content="b"), reply(content="c")])
    kwargs: dict[str, Any] = {"client": client, "thinking_levels": ("off", "low", "high")}
    await VeniceProvider("glm", thinking="high", **kwargs).complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["reasoning_effort"] == "high"
    await VeniceProvider("glm", thinking="off", **kwargs).complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["reasoning_effort"] == "none"
    # A model with no dial is sent nothing at all.
    await VeniceProvider("llama", client=client, thinking_levels=()).complete(
        system="", messages=[Message.user("hi")]
    )
    assert "reasoning_effort" not in client.request


def test_an_unlisted_model_has_no_menu() -> None:
    assert VeniceProvider.levels_for("never-listed") == ()


async def test_the_reasoning_goes_back_through_a_tool_loop() -> None:
    client = FakeClient(
        [
            reply(reasoning_content="I should echo", tool_calls=[CALL]),
            reply(reasoning_content="done now", content="echoed"),
        ]
    )
    provider = VeniceProvider("zai-org-glm-5-1", client=client)
    history: list[Message] = [Message.user("echo it")]
    first = await provider.complete(system="", messages=history, tools=[SPEC])
    history += [
        first,
        Message(role="user", content=(ToolResultBlock(tool_use_id="call_1", content="ok"),)),
    ]
    await provider.complete(system="", messages=history, tools=[SPEC])
    assistant = [m for m in client.request["messages"] if m["role"] == "assistant"]
    assert assistant == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [CALL],
            "reasoning_content": "I should echo",
        }
    ]


async def test_the_listing_says_window_ceiling_price_and_efforts() -> None:
    rows = [
        GLM,
        model("qwen-vl", capabilities={"supportsVision": True}),
        model("no-tools", capabilities={"supportsFunctionCalling": False}),
        model("down", offline=True),
        {"id": "flux", "type": "image", "model_spec": {}},
    ]
    entries = await VeniceProvider(client=FakeClient(models=rows)).list_models()
    assert entries is not None
    assert [e.id for e in entries] == ["zai-org-glm-5-1", "qwen-vl"]
    glm, vl = entries
    assert (glm.context_window, glm.max_output) == (198000, 64000)
    assert glm.thinking_levels == ("off", "low", "medium", "high")
    assert vl.thinking_levels == ()
    assert glm.modalities == ("text", "document")
    assert vl.modalities == ("text", "image", "document")
    assert glm.cost is not None
    assert (glm.cost.input, glm.cost.output, glm.cost.cache_read) == (0.9, 3.0, 0.18)
    assert glm.cost.cache_write is None
    assert glm.released == "2026-01-01"


async def test_a_retiring_model_is_deprecated_and_a_beta_one_a_preview() -> None:
    rows = [
        model(
            "kimi-k2-5",
            deprecation={"date": "2026-11-18T00:00:00.000Z", "replacementModelId": "kimi-k2-6"},
        ),
        model("new-thing", betaModel=True),
    ]
    old, new = await VeniceProvider(client=FakeClient(models=rows)).list_models() or []
    assert (old.status, old.replaced_by) == ("deprecated", "kimi-k2-6")
    assert (new.status, new.replaced_by) == ("preview", "")


def test_a_long_context_band_is_a_tier_with_every_rate() -> None:
    price = plugin.pricing_of(
        {
            "input": usd(1.42),
            "cache_input": usd(0.23),
            "output": usd(2.83),
            "extended": {
                "context_token_threshold": 200000,
                "input": usd(2.83),
                "output": usd(5.67),
                "cache_input": usd(0.45),
            },
        }
    )
    assert price is not None
    assert price.tiers == (
        PriceTier(0, 200000, 1.42, 2.83, 0.23, 1.42),
        PriceTier(200000, None, 2.83, 5.67, 0.45, 2.83),
    )


def test_a_row_without_a_price_is_unknown_not_free() -> None:
    assert plugin.pricing_of({"input": usd(1.0)}) is None
    assert plugin.pricing_of({}) is None


async def test_the_cache_read_and_write_are_split_out() -> None:
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 600, "cache_creation_input_tokens": 300},
    }
    turn = await VeniceProvider(
        "claude-sonnet-5", client=FakeClient([reply(content="hi")], usage=usage)
    ).complete(system="", messages=[Message.user("hi")])
    assert turn.usage is not None
    assert (turn.usage.input_tokens, turn.usage.cache_read_tokens) == (100, 600)
    assert turn.usage.cache_write_tokens == 300


async def test_an_empty_balance_says_where_to_fix_it() -> None:
    class PaymentRequired(Exception):
        status_code = 402

    error = PaymentRequired("Error code: 402 - Insufficient USD or Diem balance")
    try:
        await VeniceProvider("zai-org-glm-5-1", client=FakeClient(fail=error)).complete(
            system="", messages=[Message.user("hi")]
        )
    except ProviderError as exc:
        assert "venice.ai/settings/api" in str(exc) and exc.__cause__ is error
    else:
        raise AssertionError("402 was not described")


async def test_venices_overflow_is_an_overflow() -> None:
    from ultron.sdk.provider import ContextOverflowError

    error = Exception(
        "Error code: 400 - {'error': \"Your request exceeds the model's maximum context.\"}"
    )
    try:
        await VeniceProvider("zai-org-glm-5-1", client=FakeClient(fail=error)).complete(
            system="", messages=[Message.user("hi")]
        )
    except ContextOverflowError:
        pass
    else:
        raise AssertionError("the overflow was not recognised")


def test_the_plugin_registers_the_provider() -> None:
    registered: dict[str, Any] = {}

    class Ctx:
        def setting(self, key: str, default: Any = None) -> Any:
            return default

        def register_provider(self, name: str, cls: Any) -> None:
            registered[name] = cls

    plugin.VenicePlugin().register(Ctx())
    assert issubclass(registered["venice"], VeniceProvider)
