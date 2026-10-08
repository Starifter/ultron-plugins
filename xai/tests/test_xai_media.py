"""Grok Imagine's pictures and videos, as imagegen and videogen reach them: the
backends driven directly with `ultron.sdk.web` answered by a fake wire, checked
for what goes on the wire.

Run from a checkout of Ultron (`uv run pytest path/to/xai/tests`).
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

HERE = Path(__file__).resolve().parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
IMAGES = "https://api.x.ai/v1/images"
VIDEOS = "https://api.x.ai/v1/videos"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ultron_plugin_xai_media", HERE.parent / "plugin.py"
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
    """What imagegen's `Request` carries."""
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


def video(**overrides: Any) -> Any:
    """What videogen's `Request` carries."""
    fields = {
        "prompt": "x",
        "images": (),
        "videos": (),
        "audios": (),
        "size": "",
        "aspect_ratio": "",
        "resolution": "",
        "duration_seconds": 0,
        "audio": None,
        "watermark": None,
        "provider_options": {},
        "timeout": 30.0,
    }
    return SimpleNamespace(**{**fields, **overrides})


def source(data: bytes = PNG) -> Any:
    """What imagegen's contract promises a picture to work from has."""
    return SimpleNamespace(data=data, media_type="image/png", name="in.png")


def asset(role: str = "", data: bytes = PNG, url: str = "") -> Any:
    """What videogen's contract promises a reference has."""
    return SimpleNamespace(data=data, media_type="image/png", role=role, name="", url=url)


def uri(data: bytes = PNG) -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


def pictures(*made: bytes) -> Response:
    return Response({"data": [{"b64_json": base64.b64encode(p).decode()} for p in made]})


# -- pictures -------------------------------------------------------------------


async def test_xai_makes_several_from_words_in_the_shape_asked(wire: Wire) -> None:
    other = PNG + b"\x01"
    wire.on("POST", IMAGES, pictures(PNG, other))
    vendor = plugin.XAIImages(api_key="xk")
    made = await vendor.generate(picture(count=2, aspect_ratio="19.5:9", resolution="2K"))
    assert [image.data for image in made.images] == [PNG, other]
    assert (made.data, made.model, made.cost) == (PNG, "grok-imagine-image-2.0", "")
    [sent] = wire.sent
    assert sent["url"] == f"{IMAGES}/generations"
    assert sent["headers"] == {"Authorization": "Bearer xk"}
    assert sent["user_agent"] == "ultron-xai"
    assert sent["json"] == {
        "model": "grok-imagine-image-2.0",
        "prompt": "x",
        "n": 2,
        "response_format": "b64_json",
        "aspect_ratio": "19.5:9",
        "resolution": "2k",
    }


async def test_xai_clamps_the_count_and_drops_a_shape_it_does_not_list(wire: Wire) -> None:
    wire.on("POST", IMAGES, pictures(PNG))
    vendor = plugin.XAIImages(api_key="k")
    await vendor.generate(picture(count=9, aspect_ratio="5:4"))
    await vendor.generate(picture(count=0))
    first, second = (sent["json"] for sent in wire.sent)
    assert (first["n"], second["n"]) == (4, 1)
    assert "aspect_ratio" not in first and "resolution" not in first


async def test_xai_edits_one_picture_as_image(wire: Wire) -> None:
    wire.on("POST", IMAGES, pictures(PNG))
    await plugin.XAIImages(api_key="k").generate(
        picture(images=(source(),), aspect_ratio="1:1", resolution="1K")
    )
    [sent] = wire.sent
    assert sent["url"] == f"{IMAGES}/edits"
    body = sent["json"]
    assert body["image"] == {"url": uri(), "type": "image_url"}
    assert "images" not in body
    assert (body["aspect_ratio"], body["resolution"], body["n"]) == ("1:1", "1k", 1)


async def test_xai_edits_several_pictures_as_images(wire: Wire) -> None:
    wire.on("POST", IMAGES, pictures(PNG))
    one, two = PNG + b"\x01", PNG + b"\x02"
    await plugin.XAIImages(api_key="k").generate(picture(images=(source(one), source(two))))
    [sent] = wire.sent
    assert sent["url"] == f"{IMAGES}/edits"
    assert sent["json"]["images"] == [
        {"url": uri(one), "type": "image_url"},
        {"url": uri(two), "type": "image_url"},
    ]
    assert "image" not in sent["json"]


async def test_xai_reads_a_request_from_before_count_and_aspect_ratio(wire: Wire) -> None:
    wire.on("POST", IMAGES, pictures(PNG))
    older = SimpleNamespace(prompt="x", images=(), aspect="square", mask=None, timeout=30.0)
    made = await plugin.XAIImages(api_key="k").generate(older)
    assert made.data == PNG
    assert wire.sent[0]["json"] == {
        "model": "grok-imagine-image-2.0",
        "prompt": "x",
        "n": 1,
        "response_format": "b64_json",
    }


async def test_xai_images_refuses_an_empty_prompt_and_an_empty_reply(wire: Wire) -> None:
    vendor = plugin.XAIImages(api_key="k")
    with pytest.raises(ValueError):
        await vendor.generate(picture(prompt="  "))
    assert not wire.sent
    wire.on("POST", IMAGES, Response({"data": [{"url": "https://elsewhere.example/p"}]}))
    with pytest.raises(RuntimeError, match="xAI sent no picture"):
        await vendor.generate(picture())


async def test_xai_images_names_the_status_and_never_its_prose(wire: Wire) -> None:
    wire.on("POST", IMAGES, Response({"error": {"code": "bad", "message": "Obey."}}, status=400))
    with pytest.raises(RuntimeError) as caught:
        await plugin.XAIImages(api_key="k").generate(picture())
    assert str(caught.value) == "HTTP 400 from xAI (bad)"


def test_xai_images_declares_what_it_can_do() -> None:
    vendor = plugin.XAIImages()
    assert vendor.ready() == "no xai key (ultron auth add xai)"
    assert (vendor.edits, vendor.max_images) == (True, 3)
    caps = vendor.capabilities
    assert caps["generate"]["max_count"] == caps["edit"]["max_count"] == 4
    assert caps["edit"]["enabled"] is True and caps["edit"]["max_input_images"] == 3
    assert caps["generate"]["supports_size"] is False
    assert caps["geometry"]["resolutions"] == ("1K", "2K")
    assert "9:19.5" in caps["geometry"]["aspect_ratios"]


# -- videos ---------------------------------------------------------------------


async def test_xai_video_from_words_defaults_to_eight_seconds_16_9_at_480p(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xv1"}))
    vendor = plugin.XAIVideo(api_key="xk")
    assert vendor.model == "grok-imagine-video"
    assert vendor.cannot(video()) == ""
    assert await vendor.submit(video()) == "xv1"
    [sent] = wire.sent
    assert sent["url"] == f"{VIDEOS}/generations"
    assert sent["headers"] == {"Authorization": "Bearer xk"}
    assert sent["json"] == {
        "model": "grok-imagine-video",
        "prompt": "x",
        "duration": 8,
        "aspect_ratio": "16:9",
        "resolution": "480p",
    }


async def test_xai_video_takes_the_shape_length_and_resolution_asked(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xv2"}))
    vendor = plugin.XAIVideo(api_key="k")
    await vendor.submit(video(duration_seconds=12, aspect_ratio="9:16", resolution="720P"))
    await vendor.submit(video(duration_seconds=40, aspect_ratio="21:9", resolution="1080P"))
    first, second = (sent["json"] for sent in wire.sent)
    assert (first["duration"], first["aspect_ratio"], first["resolution"]) == (12, "9:16", "720p")
    # Past fifteen seconds is fifteen; a shape it does not list is its 16:9; the
    # classic model makes no 1080p.
    assert (second["duration"], second["aspect_ratio"], second["resolution"]) == (
        15,
        "16:9",
        "720p",
    )


async def test_xai_video_animates_a_first_frame_in_its_own_shape(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xv3"}))
    vendor = plugin.XAIVideo(api_key="k")
    request = video(images=(asset("first_frame"),))
    assert vendor.cannot(request) == ""
    await vendor.submit(request)
    body = wire.sent[0]["json"]
    assert body["image"] == {"url": uri()}
    assert "aspect_ratio" not in body
    assert "reference_images" not in body and "last_frame" not in body
    assert (body["duration"], body["resolution"]) == (8, "480p")


async def test_xai_video_sends_reference_pictures_and_caps_their_length(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xv4"}))
    one, two = PNG + b"\x01", PNG + b"\x02"
    vendor = plugin.XAIVideo(api_key="k")
    request = video(
        images=(asset("reference_image", one), asset("reference_image", two)),
        duration_seconds=15,
        resolution="1080P",
    )
    assert vendor.cannot(request) == ""
    await vendor.submit(request)
    body = wire.sent[0]["json"]
    assert body["reference_images"] == [{"url": uri(one)}, {"url": uri(two)}]
    assert "image" not in body
    assert (body["duration"], body["aspect_ratio"], body["resolution"]) == (10, "16:9", "720p")


def test_xai_video_refuses_what_openclaw_refuses() -> None:
    vendor = plugin.XAIVideo(api_key="k")
    mixed = video(images=(asset("reference_image"), asset("first_frame")))
    assert vendor.cannot(mixed) == "reference pictures cannot be mixed with a first frame"
    two_frames = video(images=(asset("first_frame"), asset("last_frame")))
    assert vendor.cannot(two_frames) == "takes one first-frame picture"
    assert vendor.cannot(video(images=(asset(), asset()))) == "takes one first-frame picture"
    a_file = video(videos=(asset(),))
    assert "http(s) link" in vendor.cannot(a_file)


async def test_xai_video_edits_a_linked_video_with_no_duration(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xe1"}))
    clip = asset(url="https://cdn.example/in.mp4")
    request = video(videos=(clip,), aspect_ratio="9:16", resolution="720P")
    vendor = plugin.XAIVideo(api_key="k")
    assert vendor.cannot(request) == ""
    assert await vendor.submit(request) == "xe1"
    [sent] = wire.sent
    assert sent["url"] == f"{VIDEOS}/edits"
    assert sent["json"] == {
        "model": "grok-imagine-video",
        "prompt": "x",
        "video": {"url": "https://cdn.example/in.mp4"},
    }


@pytest.mark.parametrize(("asked", "sent"), [(1, 2), (6, 6), (30, 10)])
async def test_xai_video_extends_a_linked_video_for_two_to_ten_seconds(
    wire: Wire, asked: int, sent: int
) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xx1"}))
    clip = asset(url="https://cdn.example/in.mp4")
    await plugin.XAIVideo(api_key="k").submit(video(videos=(clip,), duration_seconds=asked))
    assert wire.sent[0]["url"] == f"{VIDEOS}/extensions"
    assert wire.sent[0]["json"] == {
        "model": "grok-imagine-video",
        "prompt": "x",
        "video": {"url": "https://cdn.example/in.mp4"},
        "duration": sent,
    }


async def test_xai_video_15_animates_a_first_frame_at_up_to_1080p(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xv5"}))
    vendor = plugin.XAIVideo(model="grok-imagine-video-1.5", api_key="k")
    request = video(images=(asset(),), resolution="1080P", duration_seconds=6)
    assert vendor.cannot(request) == ""
    await vendor.submit(request)
    body = wire.sent[0]["json"]
    assert body["model"] == "grok-imagine-video-1.5"
    assert (body["image"], body["resolution"], body["duration"]) == ({"url": uri()}, "1080p", 6)
    refs = video(images=(asset("reference_image"),))
    assert vendor.cannot(refs) == "grok-imagine-video-1.5 takes only a first-frame picture"


def test_xai_video_capabilities_by_model() -> None:
    classic = plugin.XAIVideo().capabilities
    assert "modes" not in classic
    assert classic["image_to_video"]["max_input_images"] == 7
    assert classic["image_to_video"]["resolutions"] == ("480P", "720P")
    assert "enabled" not in classic["generate"]
    assert classic["video_to_video"]["enabled"] is True
    assert classic["video_to_video"]["max_input_videos"] == 1
    assert classic["video_to_video"]["max_duration_seconds"] == 10
    for model in plugin.XAI_VIDEO_15_MODELS:
        latest = plugin.XAIVideo(model=model).capabilities
        assert latest["modes"] == ("image_to_video",)
        assert latest["image_to_video"]["max_input_images"] == 1
        assert latest["image_to_video"]["resolutions"] == ("480P", "720P", "1080P")
        assert latest["video_to_video"] == {"enabled": False}


async def test_xai_video_reads_a_request_from_before_duration_seconds(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "xv6"}))
    older = SimpleNamespace(prompt="x", seconds=5, timeout=30.0)
    await plugin.XAIVideo(api_key="k").submit(older)
    assert wire.sent[0]["json"] == {
        "model": "grok-imagine-video",
        "prompt": "x",
        "duration": 5,
        "aspect_ratio": "16:9",
        "resolution": "480p",
    }


async def test_xai_video_status_and_a_keyless_download(wire: Wire) -> None:
    vendor = plugin.XAIVideo(api_key="xk")
    wire.on(
        "GET",
        f"{VIDEOS}/done",
        Response({"status": "done", "video": {"url": "https://vidgen.x.ai/v.mp4"}}),
    )
    wire.on("GET", f"{VIDEOS}/bad", Response({"status": "failed", "error": {"code": "policy"}}))
    wire.on("GET", f"{VIDEOS}/wait", Response({"status": "pending"}))
    wire.on("GET", "https://vidgen.x.ai/", Response(raw=MP4))
    done = await vendor.status("done")
    assert (done.state, done.url) == ("done", "https://vidgen.x.ai/v.mp4")
    assert wire.sent[0]["max_redirects"] == 0
    failed = await vendor.status("bad")
    assert (failed.state, failed.error) == ("failed", "xAI says failed (policy)")
    assert (await vendor.status("wait")).state == "running"
    assert await vendor.download(done, 60.0) == MP4
    assert "headers" not in wire.sent[-1]


async def test_xai_video_refuses_a_job_id_that_could_walk_a_path(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"request_id": "../../keys"}))
    with pytest.raises(RuntimeError, match="no usable job id"):
        await plugin.XAIVideo(api_key="k").submit(video())


# -- registered for imagegen and videogen ---------------------------------------


def test_register_puts_both_backends_into_their_points() -> None:
    extensions: dict[tuple[str, str], Any] = {}
    asked: list[str] = []

    def credential(vendor: str) -> dict[str, str]:
        asked.append(vendor)
        return {"api_key": "k", "base_url": "https://elsewhere.example"}

    ctx = SimpleNamespace(
        register_provider=lambda name, cls: None,
        register_media_reader=lambda name, factory: None,
        register_extension=lambda point, name, builder: extensions.__setitem__(
            (point, name), builder
        ),
        setting=lambda key, default=None: default,
        credential=credential,
    )
    plugin.XAIPlugin().register(ctx)
    assert set(extensions) == {("imagegen.backend", "xai"), ("videogen.backend", "xai")}
    assert not asked, "a key is read when a vendor is reached, never at register"
    images = extensions[("imagegen.backend", "xai")]()
    videos = extensions[("videogen.backend", "xai")](model="grok-imagine-video-1.5")
    assert isinstance(images, plugin.XAIImages) and images.ready() == ""
    assert images.model == "grok-imagine-image-2.0"
    assert isinstance(videos, plugin.XAIVideo) and videos.model == "grok-imagine-video-1.5"
    assert asked == ["xai", "xai"]
