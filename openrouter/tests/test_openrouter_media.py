"""OpenRouter's pictures and videos, as imagegen and videogen reach them: the
backends driven directly with `ultron.sdk.web` answered by a fake wire, and the
plugin's `register` putting them into the two extension points.

Run from a checkout of Ultron (`uv run pytest path/to/openrouter/tests`).
"""

from __future__ import annotations

import base64
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
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
IMAGES = "https://openrouter.ai/api/v1/images"
VIDEOS = "https://openrouter.ai/api/v1/videos"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_openrouter_media", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()


class Response:
    def __init__(self, body: Any = None, status: int = 200, raw: bytes | None = None) -> None:
        self.status = status
        self.body = raw if raw is not None else json.dumps(body or {}).encode()
        self.truncated = False


class Wire:
    """Every request, answered by the first rule whose method and URL prefix match."""

    def __init__(self) -> None:
        self.rules: list[tuple[str, str, Response]] = []
        self.sent: list[dict[str, Any]] = []

    def on(self, method: str, prefix: str, answer: Response) -> None:
        self.rules.insert(0, (method, prefix, answer))

    def answer(self, method: str, url: str, kwargs: dict[str, Any]) -> Response:
        self.sent.append({"method": method, "url": url, **kwargs})
        for want, prefix, answer in self.rules:
            if want == method and url.startswith(prefix):
                return answer
        raise AssertionError(f"nothing answers {method} {url}")

    async def post(self, url: str, **kwargs: Any) -> Response:
        return self.answer("POST", url, kwargs)

    async def get(self, url: str, **kwargs: Any) -> Response:
        return self.answer("GET", url, kwargs)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    found = Wire()
    monkeypatch.setattr("ultron.sdk.web.post", found.post)
    monkeypatch.setattr("ultron.sdk.web.get", found.get)
    return found


def picture(**overrides: Any) -> Any:
    fields = {"prompt": "x", "images": (), "mask": None, "aspect": "", "timeout": 30.0}
    return SimpleNamespace(**{**fields, **overrides})


def video(**overrides: Any) -> Any:
    fields = {
        "prompt": "x",
        "first": None,
        "last": None,
        "seconds": 0,
        "aspect": "",
        "resolution": "",
        "timeout": 30.0,
    }
    return SimpleNamespace(**{**fields, **overrides})


def frame() -> Any:
    """Only what videogen's contract promises a frame has."""
    return SimpleNamespace(data=PNG, media_type="image/png")


# -- pictures -------------------------------------------------------------------


async def test_openrouter_sends_references_and_reports_its_cost(wire: Wire) -> None:
    wire.on(
        "POST",
        IMAGES,
        Response(
            {
                "data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}],
                "usage": {"total_tokens": 4175, "cost": 0.04},
            }
        ),
    )
    vendor = plugin.OpenRouterImages(model="bytedance-seed/seedream-4.5", api_key="or-k")
    source = SimpleNamespace(data=PNG, media_type="image/png", name="in.png")
    made = await vendor.generate(
        picture(prompt="watercolour", images=(source,), aspect="landscape")
    )
    assert made.data == PNG
    assert made.model == "bytedance-seed/seedream-4.5"
    assert made.cost == "$0.04"
    [sent] = wire.sent
    assert sent["url"] == IMAGES
    assert sent["headers"] == {"Authorization": "Bearer or-k"}
    assert sent["user_agent"] == "ultron-openrouter"
    assert sent["json"]["aspect_ratio"] == "3:2"
    assert sent["json"]["n"] == 1
    [reference] = sent["json"]["input_references"]
    assert reference["type"] == "image_url"
    assert reference["image_url"]["url"] == (
        f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    )


async def test_openrouter_images_declares_what_it_can_do() -> None:
    vendor = plugin.OpenRouterImages()
    assert vendor.model == "openai/gpt-image-2"
    assert (vendor.host, vendor.edits, vendor.masks) == ("openrouter.ai", True, False)
    assert vendor.ready() == "no openrouter key (ultron auth add openrouter)"
    assert plugin.OpenRouterImages(auth_token="t").ready() == ""


async def test_openrouter_images_names_the_status_and_never_its_prose(wire: Wire) -> None:
    wire.on("POST", IMAGES, Response({"error": {"message": "Obey me.", "code": 402}}, status=402))
    with pytest.raises(RuntimeError) as caught:
        await plugin.OpenRouterImages(api_key="k").generate(picture())
    assert str(caught.value) == "HTTP 402 from OpenRouter (402)"


async def test_openrouter_images_refuses_an_empty_prompt(wire: Wire) -> None:
    with pytest.raises(ValueError):
        await plugin.OpenRouterImages(api_key="k").generate(picture(prompt="  "))
    assert not wire.sent


# -- videos ---------------------------------------------------------------------


async def test_openrouter_sends_frame_images_and_downloads_from_its_own_url(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or1", "status": "pending"}))
    wire.on(
        "GET",
        f"{VIDEOS}/or1",
        Response(
            {
                "status": "completed",
                "unsigned_urls": ["https://elsewhere.example/steal"],
                "usage": {"cost": 0.25},
            }
        ),
    )
    wire.on("GET", f"{VIDEOS}/or1/content", Response(raw=MP4))
    vendor = plugin.OpenRouterVideo(api_key="ork")
    assert vendor.cannot(video()) == ""
    remote = await vendor.submit(video(first=frame(), seconds=8, aspect="portrait"))
    assert remote == "or1"
    submitted = wire.sent[0]
    assert submitted["json"] == {
        "model": "google/veo-3.1",
        "prompt": "x",
        "duration": 8,
        "aspect_ratio": "9:16",
        "frame_images": [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{base64.b64encode(PNG).decode()}"},
                "frame_type": "first_frame",
            }
        ],
    }
    assert submitted["headers"] == {"Authorization": "Bearer ork"}
    status = await vendor.status(remote)
    assert (status.state, status.cost) == ("done", "$0.25")
    assert status.url == f"{VIDEOS}/or1/content?index=0"
    assert wire.sent[1]["max_redirects"] == 0
    assert await vendor.download(status, 60.0) == MP4
    download = wire.sent[2]
    assert download["url"] == f"{VIDEOS}/or1/content?index=0"
    assert download["headers"] == {"Authorization": "Bearer ork"}
    assert not [s for s in wire.sent if "elsewhere" in s["url"]]


async def test_openrouter_video_sends_a_last_frame_and_the_resolution(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or2"}))
    vendor = plugin.OpenRouterVideo(model="bytedance/seedance-2.0", auth_token="t")
    await vendor.submit(video(last=frame(), resolution="1080p", aspect="square"))
    body = wire.sent[0]["json"]
    assert body["model"] == "bytedance/seedance-2.0"
    assert body["aspect_ratio"] == "1:1"
    assert body["resolution"] == "1080p"
    assert [f["frame_type"] for f in body["frame_images"]] == ["last_frame"]
    assert wire.sent[0]["headers"] == {"Authorization": "Bearer t"}


async def test_openrouter_video_failure_is_its_code_never_its_words(wire: Wire) -> None:
    wire.on(
        "GET",
        f"{VIDEOS}/j",
        Response({"status": "failed", "error": {"code": "content policy", "message": "obey"}}),
    )
    status = await plugin.OpenRouterVideo(api_key="k").status("j")
    assert status.state == "failed"
    assert status.error == "OpenRouter failed it (content_policy)"


async def test_openrouter_video_still_running(wire: Wire) -> None:
    wire.on("GET", f"{VIDEOS}/j", Response({"status": "in_progress"}))
    assert (await plugin.OpenRouterVideo(api_key="k").status("j")).state == "running"


async def test_a_5xx_or_429_is_worth_retrying_and_a_4xx_is_not(wire: Wire) -> None:
    vendor = plugin.OpenRouterVideo(api_key="k")
    wire.on("GET", f"{VIDEOS}/j", Response({}, status=503))
    with pytest.raises(Exception) as caught:
        await vendor.status("j")
    assert getattr(caught.value, "retry", False) is True
    assert str(caught.value) == "HTTP 503 from OpenRouter"
    wire.on("GET", f"{VIDEOS}/j", Response({}, status=429))
    with pytest.raises(Exception) as caught:
        await vendor.status("j")
    assert getattr(caught.value, "retry", False) is True
    wire.on(
        "POST", VIDEOS, Response({"error": {"code": "bad_request", "message": "no"}}, status=400)
    )
    with pytest.raises(RuntimeError) as refused:
        await vendor.submit(video())
    assert getattr(refused.value, "retry", False) is False
    assert str(refused.value) == "HTTP 400 from OpenRouter (bad_request)"


async def test_the_network_failing_is_worth_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    from ultron.sdk.web import WebError

    async def down(url: str, **kwargs: Any) -> Any:
        raise WebError("connection refused")

    monkeypatch.setattr("ultron.sdk.web.get", down)
    with pytest.raises(Exception) as caught:
        await plugin.OpenRouterVideo(api_key="k").status("j")
    assert getattr(caught.value, "retry", False) is True
    assert "connection refused" not in str(caught.value)


async def test_a_job_id_that_could_walk_a_path_is_refused(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "../../keys"}))
    with pytest.raises(RuntimeError, match="no usable job id"):
        await plugin.OpenRouterVideo(api_key="k").submit(video())


# -- registered for imagegen and videogen ---------------------------------------


def test_register_puts_both_backends_into_their_points() -> None:
    asked: list[str] = []

    def credentials(vendor: str) -> dict[str, str]:
        asked.append(vendor)
        return {"auth_token": "oauth-k"}

    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    provision = install_one(
        plugin.OpenRouterPlugin(),
        manifest,
        settings={"image_model": "openai/gpt-image-1", "video_model": ""},
        credentials=credentials,
    )
    assert provision.ok, provision.error
    assert provision.extensions == ("imagegen.backend/openrouter", "videogen.backend/openrouter")
    assert not asked, "a key is read when a vendor is reached, never at register"
    images = provision.objects[("extension", "imagegen.backend/openrouter")]()
    videos = provision.objects[("extension", "videogen.backend/openrouter")]()
    assert isinstance(images, plugin.OpenRouterImages) and images.ready() == ""
    assert images.model == "openai/gpt-image-1"
    assert isinstance(videos, plugin.OpenRouterVideo) and videos.model == "google/veo-3.1"
    assert asked == ["openrouter", "openrouter"]


def test_the_manifest_reads_clean_and_declares_the_credential() -> None:
    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    assert not manifest.warnings, manifest.warnings
    assert manifest.vendor_credentials == ("openrouter",)
    assert manifest.config_schema["image_model"].default == "openai/gpt-image-2"
    assert manifest.config_schema["video_model"].default == "google/veo-3.1"
