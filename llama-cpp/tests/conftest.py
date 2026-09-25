"""No test reaches a real llama-server for `/props`: it answers nothing unless a
test says otherwise. On Windows a refused loopback connection takes two seconds,
and every request now asks `/props` for the loaded window first. Every other
URL - a managed server's `/health` - goes where it went."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from llama_cpp_testing import plugin


@pytest.fixture(autouse=True)
def no_real_props(monkeypatch: pytest.MonkeyPatch) -> None:
    real = plugin.fetch_json

    async def fetch(url: str) -> Mapping[str, Any]:
        if "/props" in url:
            return {}
        answer: Mapping[str, Any] = await real(url)
        return answer

    monkeypatch.setattr(plugin, "fetch_json", fetch)
