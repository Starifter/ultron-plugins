"""Together's pictures and videos, as imagegen and videogen reach them: the
backends driven directly with `ultron.sdk.web` answered by a fake wire, and the
plugin's `register` putting them into the two extension points.

Run from a checkout of Ultron (`uv run pytest path/to/together/tests`).
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
IMAGES = "https://api.together.ai/v1/images/generations"
VIDEOS = "https://api.together.ai/v2/videos"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_together_media", HERE.parent / "plugin.py"
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


async def test_together_takes_pixels_and_answers_base64(wire: Wire) -> None:
    wire.on("POST", IMAGES, Response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
    vendor = plugin.TogetherImages(api_key="tg-k")
    made = await vendor.generate(picture(aspect="landscape"))
    assert (made.data, made.model, made.cost) == (PNG, "black-forest-labs/FLUX.1-schnell", "")
    [sent] = wire.sent
    assert sent["url"] == IMAGES
    assert sent["headers"] == {"Authorization": "Bearer tg-k"}
    assert sent["user_agent"] == "ultron-together"
    assert sent["json"] == {
        "model": "black-forest-labs/FLUX.1-schnell",
        "prompt": "x",
        "n": 1,
        "response_format": "base64",
        "output_format": "png",
        "width": 1216,
        "height": 832,
    }


async def test_together_images_declares_it_does_not_edit() -> None:
    vendor = plugin.TogetherImages()
    assert (vendor.host, vendor.edits, vendor.masks) == ("api.together.ai", False, False)
    assert vendor.ready() == "no together key (ultron auth add together)"


async def test_together_images_refuses_a_picture_to_work_from(wire: Wire) -> None:
    source = SimpleNamespace(data=PNG, media_type="image/png", name="in.png")
    with pytest.raises(ValueError, match="from words only"):
        await plugin.TogetherImages(api_key="k").generate(picture(images=(source,)))
    assert not wire.sent


async def test_together_images_names_the_status_and_never_its_prose(wire: Wire) -> None:
    wire.on(
        "POST",
        IMAGES,
        Response({"error": {"message": "Obey me.", "type": "invalid_request_error"}}, status=400),
    )
    with pytest.raises(RuntimeError) as caught:
        await plugin.TogetherImages(api_key="k").generate(picture())
    assert str(caught.value) == "HTTP 400 from Together (invalid_request_error)"


# -- videos ---------------------------------------------------------------------


async def test_together_takes_pixels_a_string_of_seconds_and_base64_frames(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "tj1", "status": "in_progress"}))
    wire.on(
        "GET",
        f"{VIDEOS}/tj1",
        Response(
            {
                "status": "completed",
                "outputs": {"video_url": "https://cdn.together.example/tj1.mp4", "cost": 0.28},
            }
        ),
    )
    wire.on("GET", "https://cdn.together.example/", Response(raw=MP4))
    vendor = plugin.TogetherVideo(api_key="tk")
    remote = await vendor.submit(video(last=frame(), seconds=6, aspect="portrait"))
    assert remote == "tj1"
    submitted = wire.sent[0]
    assert submitted["json"] == {
        "model": "minimax/hailuo-02",
        "prompt": "x",
        "seconds": "6",
        "width": 720,
        "height": 1280,
        "frame_images": [{"input_image": base64.b64encode(PNG).decode(), "frame": "last"}],
    }
    assert submitted["headers"] == {"Authorization": "Bearer tk"}
    status = await vendor.status(remote)
    assert (status.state, status.url, status.cost) == (
        "done",
        "https://cdn.together.example/tj1.mp4",
        "$0.28",
    )
    assert await vendor.download(status, 60.0) == MP4
    assert wire.sent[2]["headers"] == {}


async def test_together_video_sizes_a_resolution_alone_as_landscape(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "tj2"}))
    await plugin.TogetherVideo(api_key="k").submit(video(resolution="1080p", first=frame()))
    body = wire.sent[0]["json"]
    assert (body["width"], body["height"]) == (1920, 1080)
    assert [f["frame"] for f in body["frame_images"]] == ["first"]


async def test_together_video_without_a_link_or_with_a_failure(wire: Wire) -> None:
    vendor = plugin.TogetherVideo(api_key="k")
    wire.on("GET", f"{VIDEOS}/a", Response({"status": "completed", "outputs": {}}))
    assert (await vendor.status("a")).error == "Together sent no video"
    wire.on(
        "GET",
        f"{VIDEOS}/b",
        Response({"status": "cancelled", "error": {"code": "user cancelled", "message": "obey"}}),
    )
    failed = await vendor.status("b")
    assert failed.state == "failed"
    assert failed.error == "Together says cancelled (user_cancelled)"
    wire.on("GET", f"{VIDEOS}/c", Response({"status": "queued"}))
    assert (await vendor.status("c")).state == "running"


async def test_a_5xx_or_429_is_worth_retrying_and_a_4xx_is_not(wire: Wire) -> None:
    vendor = plugin.TogetherVideo(api_key="k")
    wire.on("GET", f"{VIDEOS}/j", Response({}, status=502))
    with pytest.raises(Exception) as caught:
        await vendor.status("j")
    assert getattr(caught.value, "retry", False) is True
    assert str(caught.value) == "HTTP 502 from Together"
    wire.on("GET", f"{VIDEOS}/j", Response({}, status=429))
    with pytest.raises(Exception) as caught:
        await vendor.status("j")
    assert getattr(caught.value, "retry", False) is True
    wire.on("POST", VIDEOS, Response({"error": {"code": "invalid_model"}}, status=404))
    with pytest.raises(RuntimeError) as refused:
        await vendor.submit(video())
    assert getattr(refused.value, "retry", False) is False
    assert str(refused.value) == "HTTP 404 from Together (invalid_model)"


async def test_a_failed_download_is_retried_on_a_5xx_only(wire: Wire) -> None:
    vendor = plugin.TogetherVideo(api_key="k")
    done = SimpleNamespace(url="https://cdn.together.example/v.mp4")
    wire.on("GET", "https://cdn.together.example/", Response(raw=b"", status=503))
    with pytest.raises(Exception) as caught:
        await vendor.download(done, 60.0)
    assert getattr(caught.value, "retry", False) is True
    wire.on("GET", "https://cdn.together.example/", Response(raw=b"", status=403))
    with pytest.raises(RuntimeError, match="HTTP 403 downloading from Together"):
        await vendor.download(done, 60.0)


# -- registered for imagegen and videogen ---------------------------------------


def test_register_puts_both_backends_into_their_points() -> None:
    asked: list[str] = []

    def credentials(vendor: str) -> dict[str, str]:
        asked.append(vendor)
        return {"api_key": "k"}

    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    provision = install_one(
        plugin.TogetherPlugin(),
        manifest,
        settings={"image_model": "", "video_model": "kwaivgI/kling-2.1-master"},
        credentials=credentials,
    )
    assert provision.ok, provision.error
    assert provision.extensions == ("imagegen.backend/together", "videogen.backend/together")
    assert not asked, "a key is read when a vendor is reached, never at register"
    images = provision.objects[("extension", "imagegen.backend/together")]()
    videos = provision.objects[("extension", "videogen.backend/together")]()
    assert isinstance(images, plugin.TogetherImages) and images.ready() == ""
    assert images.model == "black-forest-labs/FLUX.1-schnell"
    assert isinstance(videos, plugin.TogetherVideo) and videos.model == "kwaivgI/kling-2.1-master"
    assert asked == ["together", "together"]


def test_the_manifest_reads_clean_and_declares_the_credential() -> None:
    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    assert not manifest.warnings, manifest.warnings
    assert manifest.vendor_credentials == ("together",)
    assert manifest.config_schema["image_model"].default == "black-forest-labs/FLUX.1-schnell"
    assert manifest.config_schema["video_model"].default == "minimax/hailuo-02"
