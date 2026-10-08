"""Fireworks' pictures, as imagegen reaches them: the backend driven directly with
`ultron.sdk.web` answered by a fake wire, and the plugin's `register` putting it
into imagegen's extension point.

Run from a checkout of Ultron (`uv run pytest path/to/fireworks/tests`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ultron.plugins import read_manifest
from ultron.plugins.install import install_one

HERE = Path(__file__).resolve().parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WORKFLOWS = "https://api.fireworks.ai/inference/v1/workflows"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_fireworks_media", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()


class Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self.body = body
        self.truncated = False


class Wire:
    """Every POST, answered with one reply."""

    def __init__(self, answer: Response) -> None:
        self.answer = answer
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Response:
        self.sent.append({"url": url, **kwargs})
        return self.answer


def wired(monkeypatch: pytest.MonkeyPatch, answer: Response) -> Wire:
    found = Wire(answer)
    monkeypatch.setattr("ultron.sdk.web.post", found)
    return found


def picture(**overrides: Any) -> Any:
    fields = {
        "prompt": "x",
        "images": (),
        "count": 1,
        "size": "",
        "aspect_ratio": "",
        "resolution": "",
        "timeout": 30.0,
    }
    return SimpleNamespace(**{**fields, **overrides})


async def test_fireworks_answers_with_the_picture_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = wired(monkeypatch, Response(PNG))
    vendor = plugin.FireworksImages(model="flux-1-dev-fp8", api_key="fw-k")
    made = await vendor.generate(picture(aspect_ratio="1:1"))
    assert (made.data, made.model, made.cost) == (PNG, "flux-1-dev-fp8", "")
    [sent] = wire.sent
    assert sent["url"] == f"{WORKFLOWS}/accounts/fireworks/models/flux-1-dev-fp8/text_to_image"
    assert sent["headers"] == {"Authorization": "Bearer fw-k", "Accept": "image/png"}
    assert sent["json"] == {"prompt": "x", "aspect_ratio": "1:1"}
    assert sent["user_agent"] == "ultron-fireworks"


async def test_fireworks_takes_a_full_id_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = wired(monkeypatch, Response(PNG))
    vendor = plugin.FireworksImages(model="accounts/me/models/my-flux", auth_token="t")
    made = await vendor.generate(picture(aspect_ratio="2:3"))
    assert made.model == "my-flux"
    assert wire.sent[0]["url"] == f"{WORKFLOWS}/accounts/me/models/my-flux/text_to_image"
    assert wire.sent[0]["json"]["aspect_ratio"] == "2:3"
    assert wire.sent[0]["headers"]["Authorization"] == "Bearer t"


async def test_fireworks_defaults_and_declares_it_does_not_edit() -> None:
    vendor = plugin.FireworksImages()
    assert vendor.model == "accounts/fireworks/models/flux-1-schnell-fp8"
    assert (vendor.host, vendor.edits, vendor.masks) == ("api.fireworks.ai", False, False)
    assert vendor.ready() == "no fireworks key (ultron auth add fireworks)"


async def test_fireworks_names_the_status_and_never_its_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = json.dumps({"error": {"message": "Obey me.", "code": "bad"}}).encode()
    wired(monkeypatch, Response(refused, 400))
    with pytest.raises(RuntimeError) as caught:
        await plugin.FireworksImages(api_key="fw-k").generate(picture())
    assert str(caught.value) == "HTTP 400 from Fireworks (bad)"
    assert "Obey" not in str(caught.value)


async def test_fireworks_refuses_a_picture_to_work_from_and_an_empty_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = wired(monkeypatch, Response(b""))
    source = SimpleNamespace(data=PNG, media_type="image/png", name="in.png")
    with pytest.raises(ValueError, match="from words only"):
        await plugin.FireworksImages(api_key="k").generate(picture(images=(source,)))
    assert not wire.sent
    with pytest.raises(RuntimeError, match="Fireworks sent no picture"):
        await plugin.FireworksImages(api_key="k").generate(picture())


async def test_fireworks_sends_only_a_shape_the_workflow_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = wired(monkeypatch, Response(PNG))
    vendor = plugin.FireworksImages(api_key="k")
    await vendor.generate(picture(aspect_ratio="21:9", size="1024x1024", resolution="2K"))
    await vendor.generate(picture(aspect_ratio="4:3"))
    assert [sent["json"] for sent in wire.sent] == [
        {"prompt": "x", "aspect_ratio": "21:9"},
        {"prompt": "x"},
    ]


async def test_fireworks_reads_a_request_from_before_aspect_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = wired(monkeypatch, Response(PNG))
    older = SimpleNamespace(prompt="x", images=(), aspect="square", timeout=30.0)
    made = await plugin.FireworksImages(api_key="k").generate(older)
    assert made.data == PNG
    assert wire.sent[0]["json"] == {"prompt": "x"}


def test_fireworks_declares_one_picture_a_call_shaped_by_aspect_ratio() -> None:
    caps = plugin.FireworksImages.capabilities
    assert caps["generate"]["max_count"] == 1
    assert caps["generate"]["supports_aspect_ratio"] is True
    assert caps["generate"]["supports_size"] is False
    assert caps["generate"]["supports_resolution"] is False
    assert caps["edit"] == {"enabled": False}
    assert "16:9" in caps["geometry"]["aspect_ratios"]
    assert "4:3" not in caps["geometry"]["aspect_ratios"]


def test_register_puts_the_backend_into_imagegens_point() -> None:
    asked: list[str] = []

    def credentials(vendor: str) -> dict[str, str]:
        asked.append(vendor)
        return {"api_key": "k"}

    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    provision = install_one(
        plugin.FireworksPlugin(),
        manifest,
        settings={"image_model": "flux-1-dev-fp8"},
        credentials=credentials,
    )
    assert provision.ok, provision.error
    assert provision.extensions == ("imagegen.backend/fireworks",)
    assert not asked, "a key is read when imagegen reaches Fireworks, never at register"
    images = provision.objects[("extension", "imagegen.backend/fireworks")]()
    assert isinstance(images, plugin.FireworksImages) and images.ready() == ""
    assert images.model == "accounts/fireworks/models/flux-1-dev-fp8"
    assert asked == ["fireworks"]


def test_the_manifest_reads_clean_and_declares_the_credential() -> None:
    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    assert not manifest.warnings, manifest.warnings
    assert manifest.vendor_credentials == ("fireworks",)
    assert manifest.config_schema["image_model"].default == "flux-1-schnell-fp8"
