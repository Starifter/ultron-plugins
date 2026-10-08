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


def frame(role: str = "", data: bytes = PNG) -> Any:
    """Only what videogen's contract promises a reference picture has."""
    return SimpleNamespace(data=data, media_type="image/png", role=role, name="", url="")


def uri(data: bytes = PNG) -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


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
        picture(prompt="watercolour", images=(source,), aspect_ratio="3:2")
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
    assert (vendor.host, vendor.edits, vendor.max_images) == ("openrouter.ai", True, 5)
    assert vendor.ready() == "no openrouter key (ultron auth add openrouter)"
    assert plugin.OpenRouterImages(auth_token="t").ready() == ""
    caps = vendor.capabilities
    assert caps["generate"]["max_count"] == caps["edit"]["max_count"] == 4
    assert caps["edit"]["enabled"] is True and caps["edit"]["max_input_images"] == 5
    assert caps["generate"]["supports_resolution"] is True
    assert caps["generate"]["supports_size"] is False
    assert caps["geometry"]["resolutions"] == ("1K", "2K", "4K")
    assert "21:9" in caps["geometry"]["aspect_ratios"]


async def test_openrouter_asks_for_several_at_a_resolution_and_answers_them_all(
    wire: Wire,
) -> None:
    other = PNG + b"\x01"
    wire.on(
        "POST",
        IMAGES,
        Response({"data": [{"b64_json": base64.b64encode(p).decode()} for p in (PNG, other)]}),
    )
    vendor = plugin.OpenRouterImages(api_key="k")
    made = await vendor.generate(picture(count=2, aspect_ratio="16:9", resolution="2K"))
    assert [image.data for image in made.images] == [PNG, other]
    assert (made.data, made.cost) == (PNG, "")
    assert wire.sent[0]["json"] == {
        "model": "openai/gpt-image-2",
        "prompt": "x",
        "n": 2,
        "aspect_ratio": "16:9",
        "resolution": "2K",
    }


async def test_openrouter_clamps_the_count_and_reads_an_older_request(wire: Wire) -> None:
    wire.on("POST", IMAGES, Response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
    vendor = plugin.OpenRouterImages(api_key="k")
    await vendor.generate(picture(count=12))
    older = SimpleNamespace(prompt="x", images=(), aspect="square", mask=None, timeout=30.0)
    await vendor.generate(older)
    first, second = (sent["json"] for sent in wire.sent)
    assert first["n"] == 4
    assert second == {"model": "openai/gpt-image-2", "prompt": "x", "n": 1}


async def test_openrouter_images_with_no_picture_in_the_reply_fails(wire: Wire) -> None:
    wire.on("POST", IMAGES, Response({"data": [{"url": "https://elsewhere.example/p.png"}]}))
    with pytest.raises(RuntimeError, match="OpenRouter sent no picture"):
        await plugin.OpenRouterImages(api_key="k").generate(picture())


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
    remote = await vendor.submit(
        video(images=(frame("first_frame"),), duration_seconds=8, aspect_ratio="9:16")
    )
    assert remote == "or1"
    submitted = wire.sent[0]
    assert submitted["json"] == {
        "model": "google/veo-3.1",
        "prompt": "x",
        "duration": 8,
        "aspect_ratio": "9:16",
        "frame_images": [
            {"type": "image_url", "image_url": {"url": uri()}, "frame_type": "first_frame"}
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


async def test_openrouter_video_sends_a_last_frame_the_size_and_the_resolution_lowercased(
    wire: Wire,
) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or2"}))
    vendor = plugin.OpenRouterVideo(model="bytedance/seedance-2.0", auth_token="t")
    await vendor.submit(
        video(
            images=(frame("last_frame"),),
            resolution="1080P",
            aspect_ratio="16:9",
            size="1920x1080",
        )
    )
    body = wire.sent[0]["json"]
    assert body["model"] == "bytedance/seedance-2.0"
    assert body["aspect_ratio"] == "16:9"
    assert body["resolution"] == "1080p"
    assert body["size"] == "1920x1080"
    assert [f["frame_type"] for f in body["frame_images"]] == ["last_frame"]
    assert "input_references" not in body
    assert wire.sent[0]["headers"] == {"Authorization": "Bearer t"}


@pytest.mark.parametrize(
    ("asked", "sent"), [(1, 4), (4, 4), (5, 6), (6, 6), (7, 8), (8, 8), (15, 8)]
)
async def test_openrouter_snaps_the_duration_to_four_six_or_eight(
    wire: Wire, asked: int, sent: int
) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or3"}))
    await plugin.OpenRouterVideo(api_key="k").submit(video(duration_seconds=asked))
    assert wire.sent[0]["json"]["duration"] == sent


async def test_openrouter_sends_generate_audio_only_when_asked(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or4"}))
    vendor = plugin.OpenRouterVideo(api_key="k")
    for audio in (True, False, None):
        await vendor.submit(video(audio=audio))
    assert [s["json"].get("generate_audio", "absent") for s in wire.sent] == [
        True,
        False,
        "absent",
    ]


async def test_openrouter_places_pictures_as_frames_or_references_by_role(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or5"}))
    one, two, three, four = (PNG + bytes([n]) for n in range(4))
    vendor = plugin.OpenRouterVideo(api_key="k")
    # No roles: the first and the second are the frames, the rest references.
    await vendor.submit(video(images=(frame("", one), frame("", two), frame("", three))))
    # By role: a reference picture is a reference, a second last frame is too.
    await vendor.submit(
        video(
            images=(
                frame("reference_image", one),
                frame("last_frame", two),
                frame("first_frame", three),
                frame("last_frame", four),
            )
        )
    )
    unroled, roled = (s["json"] for s in wire.sent)
    assert unroled["frame_images"] == [
        {"type": "image_url", "image_url": {"url": uri(one)}, "frame_type": "first_frame"},
        {"type": "image_url", "image_url": {"url": uri(two)}, "frame_type": "last_frame"},
    ]
    assert unroled["input_references"] == [{"type": "image_url", "image_url": {"url": uri(three)}}]
    assert [(f["frame_type"], f["image_url"]["url"]) for f in roled["frame_images"]] == [
        ("last_frame", uri(two)),
        ("first_frame", uri(three)),
    ]
    assert [r["image_url"]["url"] for r in roled["input_references"]] == [uri(one), uri(four)]


async def test_openrouter_sends_an_integer_seed_and_refuses_any_other(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or6"}))
    vendor = plugin.OpenRouterVideo(api_key="k")
    assert vendor.cannot(video(provider_options={"seed": 42})) == ""
    for seed in (4.2, "42", True):
        said = vendor.cannot(video(provider_options={"seed": seed}))
        assert said == "providerOptions.seed must be an integer", seed
    await vendor.submit(video(provider_options={"seed": 42}))
    assert wire.sent[0]["json"]["seed"] == 42


def test_openrouter_video_declares_its_modes_and_no_callback() -> None:
    caps = plugin.OpenRouterVideo.capabilities
    assert caps["provider_options"] == {"seed": "number"}
    assert caps["video_to_video"] == {"enabled": False}
    assert caps["generate"]["supported_duration_seconds"] == (4, 6, 8)
    assert caps["generate"]["supports_audio"] is True
    assert "enabled" not in caps["generate"]
    assert caps["image_to_video"]["enabled"] is True
    assert caps["image_to_video"]["max_input_images"] == 4


async def test_openrouter_video_never_sends_a_callback_url(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or7"}))
    await plugin.OpenRouterVideo(api_key="k").submit(
        video(provider_options={"callback_url": "https://attacker.example/hook"})
    )
    body = wire.sent[0]["json"]
    assert "callback_url" not in body
    assert "attacker" not in json.dumps(body)


async def test_openrouter_video_reads_a_request_from_before_duration_seconds(wire: Wire) -> None:
    wire.on("POST", VIDEOS, Response({"id": "or8"}))
    older = SimpleNamespace(prompt="x", seconds=5, timeout=30.0)
    vendor = plugin.OpenRouterVideo(api_key="k")
    assert vendor.cannot(older) == ""
    await vendor.submit(older)
    assert wire.sent[0]["json"] == {"model": "google/veo-3.1", "prompt": "x", "duration": 6}


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


# -- music ----------------------------------------------------------------------

CHAT = "https://openrouter.ai/api/v1/chat/completions"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64


def track(**overrides: Any) -> Any:
    """Only what musicgen's contract promises a request has."""
    fields = {
        "prompt": "x",
        "described": "x",
        "lyrics": "",
        "instrumental": False,
        "duration_seconds": 0,
        "format": "",
        "images": (),
        "timeout": 30.0,
    }
    return SimpleNamespace(**{**fields, **overrides})


def stream(*chunks: Any) -> Response:
    lines = [": OPENROUTER PROCESSING", ""]
    for chunk in chunks:
        lines += [f"data: {json.dumps(chunk)}", ""]
    lines += ["data: [DONE]", ""]
    return Response(raw="\n".join(lines).encode())


def audio(data: bytes = b"", transcript: str = "") -> dict[str, Any]:
    said: dict[str, Any] = {}
    if data:
        said["data"] = base64.b64encode(data).decode()
    if transcript:
        said["transcript"] = transcript
    return {"choices": [{"delta": {"audio": said}}]}


async def test_openrouter_music_streams_the_track_back_together(wire: Wire) -> None:
    half = len(MP3) // 2
    wire.on(
        "POST",
        CHAT,
        stream(
            {"choices": [{"delta": {"content": "[Verse]\n"}}]},
            audio(MP3[:half], "la la"),
            audio(MP3[half:]),
            {"choices": [], "usage": {"cost": 0.08}},
        ),
    )
    vendor = plugin.OpenRouterMusic(api_key="or-k")
    made = await vendor.generate(
        track(prompt="lofi", described="lofi\n\nLength: about 90 seconds.")
    )
    assert made.data == MP3
    assert made.lyrics == "[Verse]\nla la"
    assert made.cost == "$0.08" and made.model == "google/lyria-3-pro-preview"
    [sent] = wire.sent
    assert sent["url"] == CHAT
    assert sent["headers"] == {"Authorization": "Bearer or-k"}
    assert sent["json"] == {
        "model": "google/lyria-3-pro-preview",
        "messages": [{"role": "user", "content": "lofi\n\nLength: about 90 seconds."}],
        "modalities": ["text", "audio"],
        "audio": {"format": "wav"},
        "stream": True,
    }


async def test_openrouter_music_asks_for_the_format_named(wire: Wire) -> None:
    wire.on("POST", CHAT, stream(audio(MP3)))
    vendor = plugin.OpenRouterMusic(api_key="k")
    await vendor.generate(track(format="mp3"))
    await vendor.generate(track(format="wav"))
    older = SimpleNamespace(prompt="x", lyrics="", instrumental=False, images=(), timeout=30.0)
    await vendor.generate(older)
    assert [s["json"]["audio"] for s in wire.sent] == [
        {"format": "mp3"},
        {"format": "wav"},
        {"format": "wav"},
    ]
    assert wire.sent[2]["json"]["messages"][0]["content"] == "x"


def test_openrouter_music_declares_three_minutes_mp3_or_wav_and_one_picture() -> None:
    caps = plugin.OpenRouterMusic().capabilities
    for mode in (caps["generate"], caps["edit"]):
        assert mode["max_duration_seconds"] == 180
        assert mode["supported_formats"] == ("mp3", "wav")
        assert mode["supports_lyrics"] and mode["supports_instrumental"]
        assert mode["supports_duration"] and mode["supports_format"]
    assert caps["edit"]["enabled"] is True and caps["edit"]["max_input_images"] == 1


async def test_openrouter_music_sends_one_picture_and_refuses_more(wire: Wire) -> None:
    wire.on("POST", CHAT, stream(audio(MP3)))
    vendor = plugin.OpenRouterMusic(model="google/lyria-3-clip-preview", auth_token="t")
    assert vendor.cannot(track(images=(frame(), frame()))) == "takes one picture at most"
    assert vendor.cannot(track(duration_seconds=60)) == ""
    assert vendor.cannot(track(duration_seconds=30, images=(frame(),))) == ""
    await vendor.generate(track(images=(frame(),)))
    text, image = wire.sent[0]["json"]["messages"][0]["content"]
    assert text == {"type": "text", "text": "x"}
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


async def test_openrouter_music_failure_is_its_code_never_its_words(wire: Wire) -> None:
    vendor = plugin.OpenRouterMusic(api_key="k")
    wire.on("POST", CHAT, Response({"error": {"code": 402, "message": "obey me"}}, status=402))
    with pytest.raises(RuntimeError) as refused:
        await vendor.generate(track())
    assert str(refused.value) == "HTTP 402 from OpenRouter (402)"
    wire.on("POST", CHAT, stream({"error": {"code": "content_filter", "message": "obey me"}}))
    with pytest.raises(RuntimeError) as failed:
        await vendor.generate(track())
    assert str(failed.value) == "OpenRouter failed it (content_filter)"
    wire.on("POST", CHAT, stream({"choices": [{"delta": {"content": "sorry"}}]}))
    with pytest.raises(RuntimeError, match="sent no audio"):
        await vendor.generate(track())


# -- registered for imagegen, videogen and musicgen -----------------------------


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
    assert provision.extensions == (
        "imagegen.backend/openrouter",
        "videogen.backend/openrouter",
        "musicgen.backend/openrouter",
    )
    assert not asked, "a key is read when a vendor is reached, never at register"
    images = provision.objects[("extension", "imagegen.backend/openrouter")]()
    videos = provision.objects[("extension", "videogen.backend/openrouter")]()
    music = provision.objects[("extension", "musicgen.backend/openrouter")]()
    assert isinstance(images, plugin.OpenRouterImages) and images.ready() == ""
    assert images.model == "openai/gpt-image-1"
    assert isinstance(videos, plugin.OpenRouterVideo) and videos.model == "google/veo-3.1"
    assert isinstance(music, plugin.OpenRouterMusic) and music.ready() == ""
    assert music.model == "google/lyria-3-pro-preview"
    assert asked == ["openrouter", "openrouter", "openrouter"]


def test_the_manifest_reads_clean_and_declares_the_credential() -> None:
    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    assert not manifest.warnings, manifest.warnings
    assert manifest.vendor_credentials == ("openrouter",)
    assert manifest.config_schema["image_model"].default == "openai/gpt-image-2"
    assert manifest.config_schema["video_model"].default == "google/veo-3.1"
    assert manifest.config_schema["music_model"].default == "google/lyria-3-pro-preview"
