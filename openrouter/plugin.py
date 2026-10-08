"""OpenRouter provider - one key, every model OpenRouter routes to.

OpenRouter speaks OpenAI's Chat Completions dialect at `https://openrouter.ai/api/v1`,
so this is the SDK's OpenAI-compatible base with OpenRouter's own parts declared on
it (`plugin-sdk.md` §6.7). What is OpenRouter's alone is here: model ids of the form
`vendor/model`, the unified `reasoning` parameter that stands in for every vendor's
thinking control, the `reasoning_details` a reply carries and the next request has
to carry back, the `provider` routing block, cache breakpoints for the vendors that
want them, and a `GET /models` that says what each model costs.

Requires the `openai` package (`pip install openai`, or `pip install "ultron[openai]"`).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from ultron.sdk.oauth import LoginContext, Tokens
from ultron.sdk.openai_compat import OpenAICompatProvider
from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.provider import (
    CACHE_TTLS,
    ModelEntry,
    PriceTier,
    Pricing,
    ThinkingLevel,
)
from ultron.sdk.runtime import ConfigError, CredentialError

BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_REFERER = "https://github.com/Starifter/ultron"
DEFAULT_TITLE = "Ultron"
"""OpenRouter's attribution headers (`HTTP-Referer`, `X-Title`): optional, and what
lists an app on their rankings. Both are settings, so an operator can name their own."""

LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high", "max")
"""The one menu, for every model. OpenRouter's `reasoning.effort` is a single
parameter it maps onto whatever the model underneath has - a budget for Anthropic,
an effort for OpenAI, nothing for a model that does not reason - and a parameter a
provider does not take is dropped on the way through, never an error. So the menu
here is OpenRouter's rather than the model's, and `/think` is honest about that:
what `off` buys on a model whose reasoning is mandatory is the vendor's refusal,
answered once by `mark_thinking_mandatory` and then not offered again."""

EFFORT: dict[ThinkingLevel, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}
"""Ultron's levels in OpenRouter's words. `off` is not an effort but `enabled: false`."""

CACHED_VENDORS = {
    "anthropic": CACHE_TTLS,
    "google": ("5m",),
}
"""Vendors whose models cache only where a `cache_control` breakpoint says so, and the
lifetimes each sells. OpenAI, DeepSeek, Grok and the rest cache on their own terms,
and a breakpoint sent to them is a field they ignore - so it is not sent."""

MODALITIES = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "file": "document",
    "video": "video",
}
"""OpenRouter's `input_modalities` in the catalog's words."""

PER_MILLION = 1_000_000


class OpenRouterProvider(OpenAICompatProvider):
    """Chat Completions at OpenRouter, on the SDK's OpenAI-compatible base.

    There is no default model: naming one would be this plugin choosing for the
    user among four hundred, and the model is the user's decision. Ids are
    `vendor/model` - `anthropic/claude-sonnet-5`, `openai/gpt-5.5` - exactly as
    OpenRouter lists them, and `/model list --refresh` fetches that list.
    """

    name = "openrouter"
    label = "OpenRouter"
    api_key_env_vars = ("OPENROUTER_API_KEY",)
    base_url = BASE_URL
    thinking_levels = LEVELS
    streaming = True
    sampling = True
    """Every field of a `Sampling` is forwarded. OpenRouter drops what the model
    underneath does not take, so a temperature sent to a reasoning model costs
    nothing rather than failing the turn."""

    headers = {"HTTP-Referer": DEFAULT_REFERER, "X-Title": DEFAULT_TITLE}
    reasoning_fields = ("reasoning", "reasoning_content")
    """`reasoning` is OpenRouter's field; `reasoning_content` is the alias other
    proxies use."""
    replay_reasoning = True
    """A vendor whose thinking is signed - Claude, with tools - will not continue
    unless the `reasoning_details` it sent come back unmodified, in order."""
    takes_pdf = True

    routing: Mapping[str, Any] = {}
    """The `provider` block sent with every request: order, fallbacks, data
    collection. Kept beside `extra_body`, which is what carries it, so the
    plugin's settings can be read back off the class."""

    @classmethod
    def configured(cls, settings: Mapping[str, Any]) -> type[OpenRouterProvider]:
        """This class with the plugin's settings bound, for `register_provider`."""
        routing: dict[str, Any] = {}
        order = settings.get("provider_order")
        if isinstance(order, list | tuple) and order:
            routing["order"] = [str(slug) for slug in order]
        if settings.get("allow_fallbacks") is False:
            routing["allow_fallbacks"] = False
        collection = str(settings.get("data_collection", "") or "").strip().lower()
        if collection in ("allow", "deny"):
            routing["data_collection"] = collection
        return cls.bind(
            routing=routing,
            extra_body={"provider": dict(routing)} if routing else {},
            headers={
                "HTTP-Referer": str(settings.get("site_url", "") or DEFAULT_REFERER),
                "X-Title": str(settings.get("app_title", "") or DEFAULT_TITLE),
            },
        )

    @classmethod
    def model_families(cls) -> tuple[str, ...]:
        # Every id OpenRouter serves has a vendor in front of it.
        return ("*/*",)

    @classmethod
    def cache_ttls_for(cls, model: str) -> tuple[str, ...]:
        return CACHED_VENDORS.get(_vendor_of(model), ())

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        """OpenRouter's `reasoning` block, which rides `extra_body` beside the
        routing block because the `openai` package has no parameter for it."""
        if level == "off":
            return {"extra_body": {"reasoning": {"enabled": False}}}
        return {"extra_body": {"reasoning": {"effort": EFFORT[level]}}}

    async def resolve_model(self) -> str:
        raise ConfigError(
            "the OpenRouter provider needs an explicit model - set ULTRON_MODEL "
            "(or `model` in ~/.ultron/config.json) to an id like anthropic/claude-sonnet-5; "
            "`ultron models list` shows what OpenRouter offers"
        )

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: ids, numbers, dates and the price. The core
        keeps those and drops the rest (C8); the prose OpenRouter writes about a
        model never reaches the store."""
        return _entry_of(row)


def _vendor_of(model: str) -> str:
    return model.lower().lstrip("~").partition("/")[0]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# -- the browser sign-in -------------------------------------------------------------

AUTH_URL = "https://openrouter.ai/auth"
KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
LOGIN_TIMEOUT = 5 * 60.0
EXCHANGE_TIMEOUT = 30.0
CALLBACK_PATH = "/callback"
"""OpenRouter's PKCE sign-in, which mints an API key rather than a token.

Not OAuth as `OAuthClient` describes it - there is no client id, no `state`, the
authorize page takes `callback_url` rather than `redirect_uri`, and the exchange
is a JSON POST answering `{"key": ...}` - so this is the `oauth.md` §5.4 case: a
plugin-run flow that hands the core `Tokens` and keeps the store, the redaction,
the audit and `/auth` for itself. The key it mints never expires, so there is no
refresh and `token_url` is left empty on purpose.
"""

PostJson = Callable[[str, Mapping[str, Any]], tuple[int, Mapping[str, Any]]]


def _post_json(url: str, body: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(body)).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=EXCHANGE_TIMEOUT) as response:
            return int(response.status), _decode_json(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), _decode_json(exc.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CredentialError(f"openrouter.ai could not be reached: {exc}") from exc


def _decode_json(raw: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _pkce() -> tuple[str, str]:
    """(verifier, S256 challenge). The verifier is a local of the flow."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _code_from(landed: str, state: str) -> str:
    """The `code` in a pasted redirect URL whose `state` is this flow's, or the
    paste itself when it is a bare code. A URL carrying somebody else's `state`
    is refused: it is not the redirect this sign-in is waiting for."""
    text = landed.strip()
    if "://" not in text:
        return text
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
    if not _same(state, (query.get("state") or [""])[0]):
        raise CredentialError("that URL is not from this sign-in (state mismatch) - run it again")
    return (query.get("code") or [""])[0]


def _same(expected: str, got: str) -> bool:
    return secrets.compare_digest(expected.encode(), got.encode())


class _Callback:
    """One redirect on `127.0.0.1`, then nothing: the listener closes after the
    first request, and the request line - which carries the code - is never
    logged, as the core's own loopback never logs it."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.code = ""
        self._server = _CallbackServer(("127.0.0.1", 0), self)
        self.port = int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Where OpenRouter sends the browser. `state` rides inside it, because
        OpenRouter echoes `callback_url` verbatim and has no `state` of its own;
        the redirect that comes back carries it, and one without it is ignored."""
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}?state={self.state}"

    def wait(self, timeout: float, clock: Callable[[], float] = time.monotonic) -> str:
        deadline = clock() + timeout
        try:
            while not self.code:
                remaining = deadline - clock()
                if remaining <= 0:
                    return ""
                self._server.timeout = min(remaining, 1.0)
                self._server.handle_request()
        finally:
            self._server.server_close()
        return self.code


class _CallbackServer(http.server.HTTPServer):
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], owner: _Callback) -> None:
        self.owner = owner
        super().__init__(address, _CallbackHandler)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: _CallbackServer

    def log_message(self, format: str, *args: Any) -> None:
        """Nothing. The request line carries the code."""

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._answer(404, "Not found.")
            return
        query = urllib.parse.parse_qs(parts.query)
        if not _same(self.server.owner.state, (query.get("state") or [""])[0]):
            # A stray request on this port is not the user's redirect. Answered
            # and ignored; the listener keeps waiting for the right one.
            self._answer(400, "This request does not belong to the sign-in in progress.")
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._answer(400, "No code on this request.")
            return
        self.server.owner.code = code
        self._answer(200, "Signed in to OpenRouter. You can close this window.")

    def _answer(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def login(ctx: LoginContext) -> Tokens:
    """`ultron auth login openrouter`: the browser, the callback, the exchange."""
    return _run_login(ctx, post=_post_json, listen=_Callback)


def _run_login(
    ctx: LoginContext, *, post: PostJson, listen: Callable[[str], _Callback] | None
) -> Tokens:
    """The flow, with its two round trips handed in so a test can drive it.

    The code and the verifier are locals here: generated, spent on one POST,
    and gone. Neither is said, and the key that comes back goes into the
    return value and nowhere else. `state` is checked before anything else is
    read, on the redirect and on a paste alike, in constant time.
    """
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    callback: _Callback | None = None
    if listen is not None:
        try:
            callback = listen(state)
        except OSError:
            callback = None
    landing = (
        callback.url if callback is not None else f"http://127.0.0.1{CALLBACK_PATH}?state={state}"
    )
    url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {"callback_url": landing, "code_challenge": challenge, "code_challenge_method": "S256"}
        )
    )
    opened = ctx.open_browser(url)
    ctx.say(("opened a browser" if opened else "open this in a browser") + " to sign in:")
    ctx.say(f"\n    {url}\n")
    code = ""
    if callback is not None and opened:
        ctx.say("waiting for the sign-in to finish (Ctrl-C cancels)...")
        code = callback.wait(LOGIN_TIMEOUT)
    if not code:
        code = _code_from(
            ctx.ask_secret("paste the URL the browser landed on (or the code): "), state
        )
    if not code:
        raise CredentialError("no code was received - run the login again")
    status, body = post(
        KEYS_URL, {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
    )
    key = str(body.get("key", "") or "")
    if status != 200 or not key:
        raise CredentialError(f"OpenRouter refused the exchange: {_vendor_error(body, status)}")
    return Tokens(access=key, label="OpenRouter (browser sign-in)")


def _vendor_error(body: Mapping[str, Any], status: int) -> str:
    """The vendor's `error` (a string, or `{message}`), and nothing else."""
    error = body.get("error")
    said = str(error.get("message", "") or "") if isinstance(error, Mapping) else str(error or "")
    return f"HTTP {status}" + (f" {said}" if said else "")


# -- helpers: the listing --------------------------------------------------------------


def _rate(value: Any) -> float | None:
    """A listing's per-token price, as USD per million. OpenRouter writes prices
    as strings; `-1` is its word for a price it will not quote, which is *unknown*
    here and never zero (C6). `0` is a real price: the `:free` variants."""
    try:
        per_token = float(value)
    except (TypeError, ValueError):
        return None
    if per_token < 0:
        return None
    return per_token * PER_MILLION


def _pricing_of(pricing: Mapping[str, Any]) -> Pricing | None:
    """The four rates the catalog prices by, with a long-prompt `override` as a
    second tier where the listing declares one."""
    base = Pricing(
        input=_rate(pricing.get("prompt")),
        output=_rate(pricing.get("completion")),
        cache_read=_rate(pricing.get("input_cache_read")),
        cache_write=_rate(pricing.get("input_cache_write")),
    )
    if base.unknown:
        return None
    tiers: list[PriceTier] = []
    overrides = pricing.get("overrides")
    for override in overrides if isinstance(overrides, list) else []:
        band = _mapping(override)
        start = band.get("min_prompt_tokens")
        rates = (
            _rate(band.get("prompt", pricing.get("prompt"))),
            _rate(band.get("completion", pricing.get("completion"))),
            _rate(band.get("input_cache_read", pricing.get("input_cache_read"))),
            _rate(band.get("input_cache_write", pricing.get("input_cache_write"))),
        )
        if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
            continue
        if any(rate is None for rate in rates[:2]):
            continue
        tiers.append(
            PriceTier(
                from_tokens=start,
                to_tokens=None,
                input=rates[0] or 0.0,
                output=rates[1] or 0.0,
                cache_read=rates[2] if rates[2] is not None else 0.0,
                cache_write=rates[3] if rates[3] is not None else 0.0,
            )
        )
    if not tiers or base.input is None or base.output is None:
        return base
    # A tiered price has no holes: the base rates fill everything below the
    # first override, and the overrides run in the order the listing gave.
    tiers.sort(key=lambda tier: tier.from_tokens)
    schedule = [
        PriceTier(
            from_tokens=0,
            to_tokens=tiers[0].from_tokens,
            input=base.input,
            output=base.output,
            cache_read=base.cache_read if base.cache_read is not None else 0.0,
            cache_write=base.cache_write if base.cache_write is not None else 0.0,
        )
    ]
    for i, tier in enumerate(tiers):
        following = tiers[i + 1].from_tokens if i + 1 < len(tiers) else None
        schedule.append(
            PriceTier(
                from_tokens=tier.from_tokens,
                to_tokens=following,
                input=tier.input,
                output=tier.output,
                cache_read=tier.cache_read,
                cache_write=tier.cache_write,
            )
        )
    return Pricing(
        input=base.input,
        output=base.output,
        cache_read=base.cache_read,
        cache_write=base.cache_write,
        tiers=tuple(schedule),
    )


def _entry_of(item: Mapping[str, Any]) -> ModelEntry | None:
    """One row of `GET /models` as a catalog entry - ids, numbers and dates."""
    model_id = str(item.get("id", "") or "")
    if not model_id:
        return None
    context = int(item.get("context_length") or 0)
    top = _mapping(item.get("top_provider"))
    max_output = int(top.get("max_completion_tokens") or 0)
    architecture = _mapping(item.get("architecture"))
    inputs = architecture.get("input_modalities")
    modalities = tuple(
        MODALITIES[str(kind)]
        for kind in (inputs if isinstance(inputs, list) else [])
        if str(kind) in MODALITIES
    )
    created = item.get("created")
    released = ""
    if isinstance(created, int) and not isinstance(created, bool) and created > 0:
        released = datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%d")
    return ModelEntry(
        id=model_id,
        context_window=context,
        max_output=max_output,
        cost=_pricing_of(_mapping(item.get("pricing"))),
        modalities=modalities,
        released=released,
    )


# -- pictures and videos, for imagegen and videogen ------------------------------------
#
# OpenRouter's Images and Videos APIs, registered into `imagegen.backend` and
# `videogen.backend` (SDK 1.39) so those plugins reach OpenRouter without knowing it
# exists. The interfaces are theirs, written in their PLUGIN.md; nothing here imports
# either. Each builder reads the key when imagegen or videogen reaches OpenRouter,
# never before - a key added by hand or the `openrouter:oauth` sign-in, either of
# which OpenRouter takes as a bearer.

IMAGES_URL = f"{BASE_URL}/images"
IMAGE_ASPECTS = {"square": "1:1", "landscape": "3:2", "portrait": "2:3"}
VIDEOS_URL = f"{BASE_URL}/videos"
VIDEO_ASPECTS = {"landscape": "16:9", "portrait": "9:16", "square": "1:1"}
PICTURE_MAX_BYTES = 64 * 1024 * 1024
"""A picture comes back as base64, a third larger than its bytes."""
VIDEO_MAX_BYTES = 512 * 1024 * 1024
"""What one download may weigh. Fifteen seconds of 1080p is tens of MB."""
STATUS_MAX_BYTES = 1024 * 1024
"""A submission or a status is a small JSON document."""
POLL_TIMEOUT = 30.0
REMOTE_ID = re.compile(r"[A-Za-z0-9._:/-]{1,300}")
"""A job id, which goes into a URL path. It came from OpenRouter and sits in a file a
person can edit, so it is checked before it is used."""
CODE = re.compile(r"[^A-Za-z0-9_.-]+")
USER_AGENT = "ultron-openrouter"


class Made:
    """A picture, as imagegen reads one: the bytes, the model, a cost."""

    __slots__ = ("cost", "data", "model")

    def __init__(self, data: bytes, model: str = "", cost: str = "") -> None:
        self.data = data
        self.model = model
        self.cost = cost


class Status:
    """Where a video job stands, as videogen reads one."""

    __slots__ = ("cost", "error", "state", "url")

    def __init__(self, state: str, url: str = "", error: str = "", cost: str = "") -> None:
        self.state = state
        self.url = url
        self.error = error
        self.cost = cost


class Retry(Exception):
    """A request worth making again - the network, a 5xx, a 429. videogen retries an
    exception whose `retry` is true."""

    retry = True


class OpenRouterImages:
    """OpenRouter's Images API, pictures to work from as `input_references`."""

    name = "openrouter"
    host = "openrouter.ai"
    edits = True
    masks = False

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "openai/gpt-image-2").strip()
        self._key = api_key or auth_token or ""

    def ready(self) -> str:
        return "" if self._key else "no openrouter key (ultron auth add openrouter)"

    async def generate(self, request: Any) -> Made:
        if not request.prompt.strip():
            raise ValueError("nothing to make: the prompt is empty")
        body: dict[str, Any] = {"model": self.model, "prompt": request.prompt, "n": 1}
        if request.aspect in IMAGE_ASPECTS:
            body["aspect_ratio"] = IMAGE_ASPECTS[request.aspect]
        if request.images:
            body["input_references"] = [
                {"type": "image_url", "image_url": {"url": _data_uri(image.data, image.media_type)}}
                for image in request.images
            ]
        parsed = await _post_picture(IMAGES_URL, body, self._key, request.timeout)
        usage = parsed.get("usage")
        cost = ""
        if isinstance(usage, Mapping) and isinstance(usage.get("cost"), int | float):
            cost = f"${usage['cost']:g}"
        return Made(_first_b64(parsed), model=self.model, cost=cost)


class OpenRouterVideo:
    """OpenRouter's Videos API: `POST /videos`, then `/videos/{id}`, then the content
    from a URL this code builds rather than one the reply names."""

    name = "openrouter"
    host = "openrouter.ai"

    def __init__(
        self, *, model: str = "", api_key: str | None = None, auth_token: str | None = None
    ) -> None:
        self.model = (model or "google/veo-3.1").strip()
        self._key = api_key or auth_token or ""

    def ready(self) -> str:
        return "" if self._key else "no openrouter key (ultron auth add openrouter)"

    def cannot(self, request: Any) -> str:
        return ""

    async def submit(self, request: Any) -> str:
        body: dict[str, Any] = {"model": self.model, "prompt": request.prompt}
        if request.seconds:
            body["duration"] = request.seconds
        if request.aspect in VIDEO_ASPECTS:
            body["aspect_ratio"] = VIDEO_ASPECTS[request.aspect]
        if request.resolution:
            body["resolution"] = request.resolution
        frames = [
            {
                "type": "image_url",
                "image_url": {"url": _data_uri(frame.data, frame.media_type)},
                "frame_type": kind,
            }
            for kind, frame in (("first_frame", request.first), ("last_frame", request.last))
            if frame is not None
        ]
        if frames:
            body["frame_images"] = frames
        parsed = await _call(
            "POST", VIDEOS_URL, headers=self._headers(), timeout=request.timeout, body=body
        )
        return _remote(parsed.get("id"))

    async def status(self, remote: str) -> Status:
        parsed = await _call(
            "GET", f"{VIDEOS_URL}/{remote}", headers=self._headers(), timeout=POLL_TIMEOUT
        )
        state = str(parsed.get("status") or "")
        if state == "completed":
            usage = parsed.get("usage")
            cost = ""
            if isinstance(usage, Mapping) and isinstance(usage.get("cost"), int | float):
                cost = f"${usage['cost']:g}"
            # Our own content URL rather than the one in the reply: the key goes
            # with it, and a key goes only where this code chose to send it.
            return Status("done", url=f"{VIDEOS_URL}/{remote}/content?index=0", cost=cost)
        if state == "failed":
            error = parsed.get("error")
            code = ""
            if isinstance(error, Mapping):
                code = _code(error.get("code")) or _code(error.get("type"))
            return Status("failed", error="OpenRouter failed it" + (f" ({code})" if code else ""))
        return Status("running")

    async def download(self, status: Any, timeout: float) -> bytes:
        return await _download(status.url, self._headers(), timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}


async def _post_picture(
    url: str, body: Mapping[str, Any], key: str, timeout: float
) -> Mapping[str, Any]:
    """One request to the Images API. A refusal is the status and OpenRouter's error
    code and type - identifiers, never its prose, which would reach the model."""
    from ultron.sdk.web import post

    response = await post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
        max_bytes=PICTURE_MAX_BYTES,
        user_agent=USER_AGENT,
    )
    parsed = _decode_json(response.body)
    if response.status >= 400:
        raise RuntimeError(_failure(parsed, response.status, ("code", "type")))
    return parsed


async def _call(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    body: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """One JSON request to the Videos API. A 5xx, a 429 or the network is `Retry`;
    any other 4xx is final."""
    from ultron.sdk.web import WebError, get, post

    try:
        if method == "POST":
            response = await post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                max_bytes=STATUS_MAX_BYTES,
                user_agent=USER_AGENT,
            )
        else:
            response = await get(
                url,
                headers=headers,
                timeout=timeout,
                max_bytes=STATUS_MAX_BYTES,
                max_redirects=0,
                user_agent=USER_AGENT,
            )
    except WebError as exc:
        raise Retry(f"OpenRouter unreachable: {type(exc).__name__}") from None
    parsed = _decode_json(response.body)
    if response.status >= 500 or response.status == 429:
        raise Retry(_failure(parsed, response.status))
    if response.status >= 400:
        raise RuntimeError(_failure(parsed, response.status))
    return parsed


async def _download(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    from ultron.sdk.web import WebError, get

    try:
        response = await get(
            url, headers=headers, timeout=timeout, max_bytes=VIDEO_MAX_BYTES, user_agent=USER_AGENT
        )
    except WebError as exc:
        raise Retry(f"OpenRouter download failed: {type(exc).__name__}") from None
    if response.status >= 500 or response.status == 429:
        raise Retry(f"HTTP {response.status} downloading from OpenRouter")
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} downloading from OpenRouter")
    if response.truncated:
        raise RuntimeError(f"the video is over {VIDEO_MAX_BYTES // (1024 * 1024)} MB")
    return response.body


def _failure(
    parsed: Mapping[str, Any], status: int, keys: tuple[str, ...] = ("status", "code", "type")
) -> str:
    """The status and OpenRouter's error code - identifiers, never its prose. One
    that sends `error` as a sentence gets the status alone."""
    error = parsed.get("error")
    said: list[str] = []
    if isinstance(error, Mapping):
        said = [_code(error.get(key)) for key in keys]
    named = ", ".join(dict.fromkeys(part for part in said if part))
    return f"HTTP {status} from OpenRouter" + (f" ({named})" if named else "")


def _data_uri(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _first_b64(parsed: Mapping[str, Any]) -> bytes:
    """The first picture of an OpenAI-shaped `data: [{b64_json}]` reply."""
    rows = parsed.get("data")
    first = rows[0] if isinstance(rows, list) and rows else None
    encoded = first.get("b64_json") if isinstance(first, Mapping) else None
    if not encoded:
        raise RuntimeError("OpenRouter sent no picture")
    return base64.b64decode(str(encoded))


def _code(value: Any) -> str:
    """A vendor's error code or status as an identifier - never its prose."""
    return CODE.sub("_", str(value or "")).strip("_")[:60]


def _remote(value: Any) -> str:
    remote = str(value or "")
    if not REMOTE_ID.fullmatch(remote) or ".." in remote:
        raise RuntimeError("OpenRouter sent no usable job id")
    return remote


def _key_only(credential: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in credential.items() if k in ("api_key", "auth_token")}


class OpenRouterPlugin(Plugin):
    """The OpenRouter provider: one key, every model it routes to."""

    name = "openrouter"
    description = "The OpenRouter model provider - one key, every model it routes to."

    def register(self, ctx: PluginContext) -> None:
        settings = {
            key: ctx.setting(key)
            for key in (
                "provider_order",
                "allow_fallbacks",
                "data_collection",
                "site_url",
                "app_title",
            )
        }
        ctx.register_provider("openrouter", OpenRouterProvider.configured(settings))
        # The browser sign-in: a key minted by OpenRouter's PKCE page, stored as
        # the `openrouter:oauth` profile beside any key added by hand.
        ctx.register_login("openrouter", login)
        # OpenRouter's pictures and videos for imagegen and videogen, when this
        # Ultron has the points (SDK 1.39) - an older one still gets the provider
        # and the sign-in. The key is read when one of them reaches OpenRouter.
        if hasattr(ctx, "register_extension"):
            ctx.register_extension(
                "imagegen.backend",
                "openrouter",
                lambda: OpenRouterImages(
                    model=str(ctx.setting("image_model", "") or ""),
                    **_key_only(ctx.credential("openrouter")),
                ),
            )
            ctx.register_extension(
                "videogen.backend",
                "openrouter",
                lambda: OpenRouterVideo(
                    model=str(ctx.setting("video_model", "") or ""),
                    **_key_only(ctx.credential("openrouter")),
                ),
            )
