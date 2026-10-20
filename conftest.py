"""What every plugin's tests run under: Ultron's own suite's isolation.

A plugin's test that discovers plugins reads `~/.ultron/plugins`, and a
developer who has installed the plugin under test there gets two copies of it
- the suite on that machine is not the suite CI runs. Ultron's `isolated_home`
points `~` somewhere disposable for the same reason; this is that fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point `~` somewhere disposable, and clear the provider keys a developer
    may have exported, which the auth store would otherwise seed from."""
    from ultron.providers import known_providers, provider_class

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for provider in known_providers():
        wrapped = provider_class(provider)
        for variable in wrapped.api_key_env_vars if wrapped else ():
            monkeypatch.delenv(variable, raising=False)
    yield home
