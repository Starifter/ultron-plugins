"""The Discord adapter: Ultron's `docs/spec/channels.md` C8.

The shapes - a gateway event as an `Inbound`, a press as its label, a
reaction, who is addressed - are pure and tested as such. The session itself
- identify, heartbeat, resume, the REST calls a reply makes - is driven
against a fake Discord on loopback: an `aiohttp` app serving the REST paths
the adapter uses and one websocket that speaks the gateway's opcodes. Nothing
here reaches `discord.com`.

Run from the marketplace root with Ultron's environment:

    uv run --project ../Ultron pytest discord/tests
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from ultron_plugin_discord import (
    CUSTOM_ID_PREFIX,
    INTENTS,
    MESSAGE_CONTENT,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_RECONNECT,
    OP_RESUME,
    RUNNING,
    TOKEN_VARIABLE,
    Discord,
    DiscordTool,
    chunks,
    inbound_from_message,
    ogg_seconds,
    press_to_inbound,
    reaction_from_event,
    scrub,
    slash_command_payload,
    slash_to_inbound,
    thread_name,
)

from ultron.channels import Conversation, Inbound

pytestmark = pytest.mark.usefixtures("isolated_home")


async def until(condition: Any, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


BOT = "111"
OWNER = "222"
CHANNEL = "333"
THREAD = "444"
GUILD = "555"
DM_CHANNEL = "666"
FORUM = "777"
APP = "888"

DM = Conversation("discord", kind="direct", account="")
ROOM = Conversation("discord", kind="group", room=CHANNEL)
IN_THREAD = Conversation("discord", kind="group", room=CHANNEL, thread=THREAD)


def _author(user_id: str = OWNER, **extra: Any) -> dict[str, Any]:
    return {"id": user_id, "username": "ann", "global_name": "Ann", **extra}


# -- shapes ------------------------------------------------------------------------------


def test_a_dm_and_a_guild_message_become_inbounds() -> None:
    dm = inbound_from_message(
        {"id": "1", "type": 0, "content": "hi", "author": _author()}, DM, bot_id=BOT
    )
    assert dm is not None
    assert dm.channel == "discord" and dm.peer == OWNER and dm.kind == "direct"
    assert dm.text == "hi" and dm.raw_id == "1" and dm.display_name == "Ann"
    assert not dm.mentioned and dm.identity == f"discord:{OWNER}"

    plain = inbound_from_message(
        {"id": "2", "type": 0, "content": "hello all", "author": _author()}, ROOM, bot_id=BOT
    )
    assert plain is not None and plain.kind == "group" and plain.room == CHANNEL
    assert not plain.mentioned

    mentioned = inbound_from_message(
        {
            "id": "3",
            "type": 0,
            "content": f"<@{BOT}> look",
            "author": _author(),
            "mentions": [{"id": BOT}],
            "member": {"nick": "Annie"},
        },
        IN_THREAD,
        bot_id=BOT,
    )
    assert mentioned is not None and mentioned.mentioned
    assert mentioned.room == CHANNEL and mentioned.thread == THREAD
    assert mentioned.display_name == "Annie"

    reply = inbound_from_message(
        {
            "id": "4",
            "type": 19,
            "content": "yes",
            "author": _author(),
            "referenced_message": {"id": "9", "content": "ok?", "author": {"id": BOT}},
        },
        ROOM,
        bot_id=BOT,
    )
    assert reply is not None and reply.mentioned
    assert reply.reply_to == "9" and reply.quoted == "ok?"


def test_bots_webhooks_notices_and_empty_updates_are_nothing() -> None:
    assert (
        inbound_from_message(
            {"id": "1", "type": 0, "content": "x", "author": _author(bot=True)}, DM, bot_id=BOT
        )
        is None
    )
    assert (
        inbound_from_message(
            {"id": "1", "type": 0, "content": "x", "author": _author(), "webhook_id": "7"},
            DM,
            bot_id=BOT,
        )
        is None
    )
    # A pin notice, a join: not a message to the agent.
    assert (
        inbound_from_message({"id": "1", "type": 6, "author": _author()}, ROOM, bot_id=BOT) is None
    )
    # An update that carries no content - an embed resolving - is not an edit.
    assert (
        inbound_from_message({"id": "1", "author": _author()}, DM, bot_id=BOT, edited=True) is None
    )
    edited = inbound_from_message(
        {"id": "1", "content": "fixed", "author": _author()}, DM, bot_id=BOT, edited=True
    )
    assert edited is not None and edited.edited and edited.text == "fixed"


def test_attachments_are_named_by_kind() -> None:
    inbound = inbound_from_message(
        {
            "id": "1",
            "type": 0,
            "content": "",
            "author": _author(),
            "attachments": [
                {"filename": "cat.png", "content_type": "image/png", "size": 10},
                {"filename": "notes.pdf", "content_type": "application/pdf", "size": 10},
                {"filename": "voice-message.ogg", "content_type": "audio/ogg", "size": 10},
            ],
            "sticker_items": [{"id": "1"}],
        },
        DM,
        bot_id=BOT,
    )
    assert inbound is not None
    assert inbound.attachments == (
        "photo cat.png",
        "document notes.pdf",
        "audio voice-message.ogg",
        "sticker",
    )
    assert not inbound.empty


def test_a_press_is_its_label_and_a_strangers_custom_id_is_nothing() -> None:
    interaction = {
        "id": "i1",
        "type": 3,
        "data": {"custom_id": f"{CUSTOM_ID_PREFIX}1"},
        "message": {
            "id": "m1",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {"type": 2, "label": "Allow"},
                        {"type": 2, "label": "Deny"},
                    ],
                }
            ],
        },
        "member": {"user": _author(), "nick": ""},
    }
    remembered = press_to_inbound(interaction, ["Allow", "Deny"], DM)
    assert remembered is not None and remembered.text == "Deny" and remembered.mentioned
    assert remembered.raw_id == "press:i1" and remembered.peer == OWNER
    # After a restart the labels are read back off the message.
    forgotten = press_to_inbound(interaction, None, DM)
    assert forgotten is not None and forgotten.text == "Deny"
    other = {**interaction, "data": {"custom_id": "somebody-elses"}}
    assert press_to_inbound(other, None, DM) is None
    dm_user = {**interaction, "member": None, "user": _author("777")}
    pressed = press_to_inbound(dm_user, None, DM)
    assert pressed is not None and pressed.peer == "777"


def test_a_reaction_is_an_event_on_the_message() -> None:
    added = reaction_from_event(
        {
            "user_id": OWNER,
            "message_id": "41",
            "emoji": {"id": None, "name": "👍"},
            "member": {"user": _author()},
        },
        ROOM,
    )
    assert added is not None and added.reaction == "👍" and added.reply_to == "41"
    assert added.is_event and not added.empty and added.raw_id == "reaction:41"
    custom = reaction_from_event(
        {"user_id": OWNER, "message_id": "41", "emoji": {"id": "12", "name": "party"}}, DM
    )
    assert custom is not None and custom.reaction == "party:12"
    removed = reaction_from_event(
        {"user_id": OWNER, "message_id": "41", "emoji": {"name": "👍"}}, DM, removed=True
    )
    assert removed is not None and removed.reaction_removed and not removed.reaction


def test_accounts_from_tokens_and_the_token_never_in_an_error(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv(TOKEN_VARIABLE, raising=False)
    adapter = Discord(workspace=tmp_path)
    assert adapter.name == "discord" and adapter.can_ask and adapter.max_text == 2000
    assert adapter.credential_variable == TOKEN_VARIABLE == "DISCORD_BOT_TOKEN"
    assert not adapter.ready()
    monkeypatch.setenv(f"{TOKEN_VARIABLE}_Work", "tok.work")
    assert adapter.tokens() == {"work": "tok.work"} and adapter.ready()
    monkeypatch.setenv(TOKEN_VARIABLE, "tok.main")
    assert adapter.tokens()[""] == "tok.main"
    assert "tok.main" not in scrub("Authorization: Bot tok.main refused", "tok.main")
    assert TOKEN_VARIABLE in adapter.setup() and "discord:<numeric user id>" in adapter.setup()
    assert INTENTS & MESSAGE_CONTENT


def test_chunks_split_at_a_line() -> None:
    assert list(chunks("a" * 2500, 2000)) == ["a" * 2000, "a" * 500]
    assert list(chunks("line\n" * 3, 8)) == ["line", "line", "line\n"]
    assert list(chunks("", 2000)) == [""]


# -- the session, against a fake Discord -----------------------------------------------------


class FakeDiscord:
    """The REST paths the adapter uses and one gateway websocket. Each
    websocket connection is a `session`; what the client sent is kept per
    session, and the test pushes events with `dispatch`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.sessions: list[list[dict[str, Any]]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.heartbeat_interval_ms = 45_000
        self.ack_heartbeats = True
        self.seq = 0
        self.port = 0
        self._runner: web.AppRunner | None = None

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}/api"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/gateway/bot", self._gateway)
        app.router.add_get("/gw", self._socket)
        app.router.add_route("*", "/api/{tail:.*}", self._rest)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert self._runner.addresses
        self.port = int(self._runner.addresses[0][1])

    async def stop(self) -> None:
        for socket in self.sockets:
            if not socket.closed:
                await socket.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _gateway(self, request: web.Request) -> web.Response:
        self.calls.append(("GET", "/gateway/bot", request.headers.get("Authorization")))
        return web.json_response({"url": f"ws://127.0.0.1:{self.port}/gw"})

    async def _rest(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["tail"]
        body: Any = None
        if request.content_type.startswith("multipart/"):
            # An upload (C18.1): the message under `payload_json`, the file
            # under `files[0]`, recorded as what came apart.
            form = await request.post()
            upload = form["files[0]"]
            assert isinstance(upload, web.FileField)
            payload = form["payload_json"]
            if isinstance(payload, web.FileField):
                payload = payload.file.read()
            body = {
                **json.loads(payload),
                "_file": (upload.filename, upload.content_type, upload.file.read()),
            }
        else:
            raw = await request.read()
            if raw:
                body = json.loads(raw)
        self.calls.append((request.method, path, body))
        if path == "/users/@me/channels":
            return web.json_response({"id": DM_CHANNEL})
        if path == "/users/@me/guilds":
            return web.json_response([{"id": GUILD, "name": "Friends"}])
        if path == f"/guilds/{GUILD}/channels":
            return web.json_response(
                [
                    {"id": CHANNEL, "type": 0, "name": "general"},
                    {"id": FORUM, "type": 15, "name": "ideas"},
                ]
            )
        if path == f"/channels/{CHANNEL}/messages" and request.method == "GET":
            return web.json_response(
                [
                    {
                        "id": "52",
                        "content": "second",
                        "author": {"id": OWNER, "username": "ann"},
                        "timestamp": "2026-09-22T10:01:00Z",
                    },
                    {
                        "id": "51",
                        "content": "first",
                        "author": {"id": "999", "username": "bob"},
                        "timestamp": "2026-09-22T10:00:00Z",
                    },
                ]
            )
        if path == f"/channels/{FORUM}":
            return web.json_response({"id": FORUM, "type": 15, "name": "ideas"})
        if path == f"/channels/{FORUM}/threads":
            return web.json_response({"id": THREAD, "type": 11, "message": {"id": "post1"}})
        if path.endswith("/threads") and request.method == "POST":
            return web.json_response({"id": "t2", "type": 11})
        if path.startswith("/applications/"):
            return web.json_response(body or [])
        if path.endswith("/messages") and request.method == "POST":
            return web.json_response({"id": f"sent{len(self.calls)}"})
        if path.startswith("/channels/") and request.method == "GET":
            return web.json_response({"id": THREAD, "type": 11, "parent_id": CHANNEL})
        if request.method in ("PATCH",):
            return web.json_response({"id": path.rsplit("/", 1)[-1]})
        return web.Response(status=204)

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.append(socket)
        received: list[dict[str, Any]] = []
        self.sessions.append(received)
        await socket.send_json(
            {"op": OP_HELLO, "d": {"heartbeat_interval": self.heartbeat_interval_ms}}
        )
        async for message in socket:
            if message.type != web.WSMsgType.TEXT:
                break
            frame = json.loads(message.data)
            received.append(frame)
            if frame.get("op") == OP_IDENTIFY:
                await self.dispatch(
                    "READY",
                    {
                        "user": {"id": BOT, "username": "Ultron"},
                        "application": {"id": APP},
                        "session_id": "sess1",
                        "resume_gateway_url": f"ws://127.0.0.1:{self.port}/gw",
                    },
                    socket,
                )
            elif frame.get("op") == OP_RESUME:
                await self.dispatch("RESUMED", {}, socket)
            elif frame.get("op") == OP_HEARTBEAT and self.ack_heartbeats:
                await socket.send_json({"op": OP_HEARTBEAT_ACK})
        return socket

    async def dispatch(
        self, event: str, data: Any, socket: web.WebSocketResponse | None = None
    ) -> None:
        self.seq += 1
        target = socket or self.sockets[-1]
        await target.send_json({"op": OP_DISPATCH, "t": event, "s": self.seq, "d": data})

    def sent(self, method: str, path_end: str) -> list[Any]:
        return [body for m, p, body in self.calls if m == method and p.endswith(path_end)]


@pytest.fixture
async def fake(
    tmp_path: Path, monkeypatch: Any
) -> AsyncIterator[tuple[FakeDiscord, Discord, list[Inbound]]]:
    monkeypatch.setenv(TOKEN_VARIABLE, "tok.main")
    server = FakeDiscord()
    await server.start()
    adapter = Discord(workspace=tmp_path, api=server.api, allow_private=True)
    adapter.commands = (("status", "Where things stand.", ""), ("new", "Start over.", "[title]"))
    delivered: list[Inbound] = []

    async def deliver(inbound: Inbound) -> None:
        delivered.append(inbound)

    try:
        yield server, adapter, delivered
    finally:
        await adapter.stop()
        await server.stop()


async def test_identify_then_a_dm_arrives_and_the_reply_goes_to_its_channel(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    (session,) = server.sessions
    identify = session[0]
    assert identify["op"] == OP_IDENTIFY and identify["d"]["intents"] == INTENTS
    assert identify["d"]["token"] == "tok.main"
    assert server.calls[0][2] == "Bot tok.main"  # `/gateway/bot`, authorised
    presence = adapter.presence()["accounts"]["default"]
    assert presence["connected"] and presence["username"] == "ultron"

    await server.dispatch(
        "MESSAGE_CREATE",
        {"id": "1", "type": 0, "channel_id": DM_CHANNEL, "content": "hi", "author": _author()},
    )
    await until(lambda: len(delivered) == 1)
    inbound = delivered[0]
    assert inbound.kind == "direct" and inbound.peer == OWNER and inbound.text == "hi"

    handle = await adapter.send(inbound.conversation, "hello **there**")
    assert handle
    (body,) = server.sent("POST", f"/channels/{DM_CHANNEL}/messages")
    assert body["content"] == "hello **there**" and "message_reference" not in body
    assert body["allowed_mentions"]["parse"] == ["users"]
    # No DM channel was opened: the one the message came in on is used.
    assert not server.sent("POST", "/users/@me/channels")

    await adapter.edit(inbound.conversation, handle, "hello there!")
    (edited,) = server.sent("PATCH", f"/messages/{handle}")
    assert edited["content"] == "hello there!"
    await adapter.typing(inbound.conversation)
    assert server.sent("POST", f"/channels/{DM_CHANNEL}/typing")


async def test_a_dm_nobody_sent_yet_opens_a_channel_first(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await adapter.send(Conversation("discord", peer="999"), "knock")
    (opened,) = server.sent("POST", "/users/@me/channels")
    assert opened == {"recipient_id": "999"}
    assert server.sent("POST", f"/channels/{DM_CHANNEL}/messages")


async def test_send_media_is_one_multipart_message_with_the_file(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
    handle = await adapter.send_media(
        Conversation("discord", kind="group", room=CHANNEL, message="7"),
        png,
        "chart.png",
        "image/png",
        "the chart",
    )
    assert handle
    (posted,) = [b for b in server.sent("POST", f"/channels/{CHANNEL}/messages") if "_file" in b]
    assert posted["content"] == "the chart"
    assert posted["attachments"] == [{"id": 0, "filename": "chart.png"}]
    assert posted["message_reference"]["message_id"] == "7"
    assert posted["_file"] == ("chart.png", "image/png", png)
    # A caption over the message limit follows as a message of its own.
    long = "x" * 2500
    await adapter.send_media(
        Conversation("discord", peer=OWNER), b"%PDF-", "r.pdf", "application/pdf", long
    )
    bodies = server.sent("POST", f"/channels/{DM_CHANNEL}/messages")
    # The file first with no caption, then the caption in two chunks.
    assert bodies[-3]["content"] == "" and "_file" in bodies[-3]
    assert "".join(b["content"] for b in bodies[-2:]) == long
    assert all("_file" not in b for b in bodies[-2:])
    with pytest.raises(RuntimeError):
        await adapter.send_media(
            Conversation("discord", peer=OWNER), b"x" * 10_000_001, "big", "application/zip"
        )


async def test_send_voice_is_a_voice_message_with_no_content(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    """`tts.md` R10.1: the flag, an Ogg attachment with its length and a
    waveform, and no content - a caption, when one is given, follows."""
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    # One Ogg page whose granule position is 96 000 samples: two seconds.
    ogg = b"OggS\x00\x04" + (96_000).to_bytes(8, "little") + b"\x00" * 64
    assert ogg_seconds(ogg) == 2.0 and ogg_seconds(b"not ogg") == 0.0
    assert adapter.voice_types == ("audio/ogg",) and adapter.voice_caption is False
    handle = await adapter.send_voice(Conversation("discord", peer=OWNER), ogg, "audio/ogg")
    assert handle
    (posted,) = [b for b in server.sent("POST", f"/channels/{DM_CHANNEL}/messages") if "_file" in b]
    assert posted["flags"] == 1 << 13 and "content" not in posted
    (attachment,) = posted["attachments"]
    assert attachment["duration_secs"] == 2.0 and attachment["waveform"]
    assert posted["_file"] == ("voice-message.ogg", "audio/ogg", ogg)
    with pytest.raises(RuntimeError):
        await adapter.send_voice(Conversation("discord", peer=OWNER), b"ID3", "audio/mpeg")


async def test_a_thread_message_is_routed_under_its_room_and_a_reply_references_it(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await server.dispatch(
        "MESSAGE_CREATE",
        {
            "id": "7",
            "type": 0,
            "guild_id": GUILD,
            "channel_id": THREAD,
            "content": f"<@{BOT}> hey",
            "mentions": [{"id": BOT}],
            "author": _author(),
        },
    )
    await until(lambda: len(delivered) == 1)
    inbound = delivered[0]
    # The thread was unknown, so the adapter asked once and remembered.
    assert server.sent("GET", f"/channels/{THREAD}") == [None]
    assert inbound.kind == "group" and inbound.room == CHANNEL and inbound.thread == THREAD
    assert inbound.mentioned
    await adapter.send(inbound.conversation.at("7"), "sure")
    (body,) = server.sent("POST", f"/channels/{THREAD}/messages")
    assert body["message_reference"] == {"message_id": "7", "fail_if_not_exists": False}
    assert body["allowed_mentions"]["replied_user"] is False

    await adapter.react(inbound.conversation, "7", "👀")
    assert (
        "PUT",
        f"/channels/{THREAD}/messages/7/reactions/👀/@me",
        None,
    ) in server.calls
    await adapter.react(inbound.conversation, "7", "")
    assert ("DELETE", f"/channels/{THREAD}/messages/7/reactions", None) in server.calls


async def test_choices_are_buttons_and_a_press_is_answered_and_taken_off(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    conversation = Conversation("discord", kind="group", room=CHANNEL)
    handle = await adapter.send_choices(conversation, "Allow it?", ["Allow", "Deny"])
    (body,) = server.sent("POST", f"/channels/{CHANNEL}/messages")
    buttons = body["components"][0]["components"]
    assert [b["label"] for b in buttons] == ["Allow", "Deny"]
    assert buttons[1]["custom_id"] == f"{CUSTOM_ID_PREFIX}1"
    await server.dispatch(
        "INTERACTION_CREATE",
        {
            "id": "i9",
            "token": "itok",
            "type": 3,
            "guild_id": GUILD,
            "channel_id": CHANNEL,
            "data": {"custom_id": f"{CUSTOM_ID_PREFIX}1"},
            "message": {"id": handle},
            "member": {"user": _author()},
        },
    )
    await until(lambda: len(delivered) == 1)
    assert delivered[0].text == "Deny" and delivered[0].peer == OWNER
    (answer,) = server.sent("POST", "/interactions/i9/itok/callback")
    assert answer == {"type": 7, "data": {"components": []}}


async def test_a_deletion_is_recorded_as_its_authors_and_a_reaction_arrives(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await server.dispatch(
        "MESSAGE_CREATE",
        {"id": "5", "type": 0, "channel_id": DM_CHANNEL, "content": "oops", "author": _author()},
    )
    await server.dispatch("MESSAGE_DELETE", {"id": "5", "channel_id": DM_CHANNEL})
    await server.dispatch("MESSAGE_DELETE", {"id": "unknown", "channel_id": DM_CHANNEL})
    await server.dispatch(
        "MESSAGE_REACTION_ADD",
        {"user_id": OWNER, "channel_id": DM_CHANNEL, "message_id": "5", "emoji": {"name": "👍"}},
    )
    # The bot's own reaction never comes back as an event.
    await server.dispatch(
        "MESSAGE_REACTION_ADD",
        {"user_id": BOT, "channel_id": DM_CHANNEL, "message_id": "5", "emoji": {"name": "👀"}},
    )
    await until(lambda: len(delivered) == 3)
    await asyncio.sleep(0.05)
    assert len(delivered) == 3
    deleted = delivered[1]
    assert deleted.deleted and deleted.raw_id == "5" and deleted.peer == OWNER
    assert delivered[2].reaction == "👍" and delivered[2].reply_to == "5"


async def test_reconnect_resumes_with_the_session_and_sequence(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await server.dispatch(
        "MESSAGE_CREATE",
        {"id": "1", "type": 0, "channel_id": DM_CHANNEL, "content": "a", "author": _author()},
    )
    await until(lambda: len(delivered) == 1)
    seq = server.seq
    await server.sockets[-1].send_json({"op": OP_RECONNECT})
    await until(lambda: len(server.sessions) == 2)
    await until(lambda: len(server.sessions[1]) >= 1)
    resume = server.sessions[1][0]
    assert resume["op"] == OP_RESUME
    assert resume["d"] == {"token": "tok.main", "session_id": "sess1", "seq": seq}
    await until(lambda: adapter._bots[""].connected)
    # And the resumed session carries messages as before.
    await server.dispatch(
        "MESSAGE_CREATE",
        {"id": "2", "type": 0, "channel_id": DM_CHANNEL, "content": "b", "author": _author()},
    )
    await until(lambda: len(delivered) == 2)


async def test_a_heartbeat_nobody_acks_is_a_zombie_and_is_resumed(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    server.heartbeat_interval_ms = 100
    server.ack_heartbeats = False
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await until(lambda: any(f.get("op") == OP_HEARTBEAT for f in server.sessions[0]), timeout=3.0)
    await until(lambda: len(server.sessions) >= 2, timeout=10.0)
    await until(lambda: len(server.sessions[1]) >= 1)
    assert server.sessions[1][0]["op"] == OP_RESUME


async def test_a_refused_token_stops_the_account_and_says_so(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await server.sockets[-1].close(code=4004, message=b"Authentication failed")
    await until(lambda: bool(adapter._bots[""].fatal))
    await asyncio.sleep(0.05)
    assert len(server.sessions) == 1
    presence = adapter.presence()["accounts"]["default"]
    assert presence["stopped"] and "token" in presence["stopped"] and not presence["connected"]


def _collect(into: list[Inbound]) -> Any:
    async def deliver(inbound: Inbound) -> None:
        into.append(inbound)

    return deliver


# -- forums, slash commands, the tool (C8.4 - C8.6) -----------------------------------------


def test_thread_name_is_the_first_line_cut_to_the_limit() -> None:
    assert thread_name("\n# Big idea\nbody") == "Big idea"
    assert thread_name("x" * 200) == "x" * 100
    assert thread_name("  \n ") == "post"


def test_slash_command_payload_keeps_what_discord_accepts() -> None:
    payload = slash_command_payload(
        [("status", "Where things stand.", ""), ("Bad Name", "x", ""), ("new", "", "[title]")]
    )
    assert [c["name"] for c in payload] == ["status", "new"]
    assert payload[0]["description"] == "Where things stand." and payload[0]["type"] == 1
    assert payload[1]["description"] == "/new"
    assert payload[1]["options"][0] == {
        "type": 3,
        "name": "args",
        "description": "[title]",
        "required": False,
    }
    assert len(slash_command_payload([(f"c{i}", "", "") for i in range(150)])) == 100
    assert slash_command_payload(()) == []


def test_tts_is_voice_in_discords_menu_and_back() -> None:
    """`tts.md` R11.3: `/tts` is Discord's own, so Ultron's is `/voice` there,
    and a press of it is `/tts` to the core."""
    (entry,) = slash_command_payload([("tts", "Speak replies aloud.", "[on|off]")])
    assert entry["name"] == "voice"
    inbound = slash_to_inbound(
        {
            "data": {"name": "voice", "options": [{"name": "args", "value": "on"}]},
            "user": _author(),
        },
        DM,
    )
    assert inbound is not None and inbound.text == "/tts on"


def test_a_slash_interaction_is_the_command_line() -> None:
    inbound = slash_to_inbound(
        {
            "id": "i5",
            "type": 2,
            "data": {"name": "status", "options": [{"name": "args", "value": " -v "}]},
            "member": {"user": _author()},
        },
        ROOM,
    )
    assert inbound is not None and inbound.text == "/status -v" and inbound.is_command
    assert inbound.mentioned and inbound.raw_id == "command:i5" and inbound.kind == "group"
    bare = slash_to_inbound({"data": {"name": "new"}, "user": _author("5")}, DM)
    assert bare is not None and bare.text == "/new" and bare.peer == "5"
    assert slash_to_inbound({"data": {}, "user": _author()}, DM) is None


async def test_a_forum_post_starts_a_thread_and_edits_follow_it(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    forum = Conversation("discord", kind="group", room=FORUM)
    long = await adapter.send(forum, "# Big idea\nWhat if...\n" + "x" * 2100)
    assert long and long != "post1"  # the last chunk's id, as any split send returns
    (started,) = server.sent("POST", f"/channels/{FORUM}/threads")
    assert started["name"] == "Big idea" and started["message"]["content"].startswith("# Big idea")
    # The remainder went into the new thread, not the forum.
    (rest,) = server.sent("POST", f"/channels/{THREAD}/messages")
    assert rest["content"].startswith("x")
    assert not server.sent("POST", f"/channels/{FORUM}/messages")
    # A post that fits one message is its own handle, and an edit of it goes to the thread.
    assert await adapter.send(forum, "Small idea") == "post1"
    await adapter.edit(forum, "post1", "# Big idea\nWhat if, edited")
    (edited,) = server.sent("PATCH", "/messages/post1")
    assert ("PATCH", f"/channels/{THREAD}/messages/post1", edited) in server.calls


async def test_slash_commands_are_registered_at_ready_and_a_pick_runs_as_typed(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await until(lambda: bool(server.sent("PUT", f"/applications/{APP}/commands")))
    (registered,) = server.sent("PUT", f"/applications/{APP}/commands")
    assert [c["name"] for c in registered] == ["status", "new"]
    await server.dispatch(
        "INTERACTION_CREATE",
        {
            "id": "i7",
            "token": "itok",
            "type": 2,
            "channel_id": DM_CHANNEL,
            "data": {"name": "new", "options": [{"name": "args", "value": "fresh"}]},
            "user": _author(),
        },
    )
    await until(lambda: len(delivered) == 1)
    assert delivered[0].text == "/new fresh" and delivered[0].kind == "direct"
    (answer,) = server.sent("POST", "/interactions/i7/itok/callback")
    assert answer["type"] == 4 and answer["data"]["flags"] == 64
    assert "/new fresh" in answer["data"]["content"]
    # The DM channel was learnt from the interaction, so the reply needs no lookup.
    await adapter.send(delivered[0].conversation, "started")
    assert server.sent("POST", f"/channels/{DM_CHANNEL}/messages")
    assert not server.sent("POST", "/users/@me/channels")


async def test_slash_commands_off_registers_nothing(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    adapter.slash_commands = False
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    await asyncio.sleep(0.05)
    assert not server.sent("PUT", f"/applications/{APP}/commands")


async def test_the_discord_tool_acts_through_the_running_adapter(
    fake: tuple[FakeDiscord, Discord, list[Inbound]],
) -> None:
    server, adapter, delivered = fake
    tool = DiscordTool()
    assert tool.untrusted and tool.name == "discord"
    absent = await tool.run(action="servers")
    assert absent.is_error and "no Discord account" in absent.content
    await adapter.start(_collect(delivered))
    await until(lambda: any(bot.connected for bot in adapter._bots.values()))
    assert RUNNING["adapter"] is adapter

    servers = await tool.run(action="servers")
    assert not servers.is_error and f"{GUILD}  Friends" in servers.content
    channels = await tool.run(action="channels", server=GUILD)
    assert f"{FORUM}  forum  #ideas" in channels.content
    history = await tool.run(action="history", channel=CHANNEL, limit=2)
    assert history.content.index("[51]") < history.content.index("[52]")
    assert "bob: first" in history.content and "second" in history.content
    pinned = await tool.run(action="pin", channel=CHANNEL, message="51")
    assert not pinned.is_error
    assert ("PUT", f"/channels/{CHANNEL}/pins/51", None) in server.calls
    thread = await tool.run(action="thread_create", channel=CHANNEL, name="Plans", message="51")
    assert f"thread:discord:{CHANNEL}:t2" in thread.content
    poll = await tool.run(
        action="poll", channel=CHANNEL, question="Lunch?", answers=["Pizza", "Sushi"], hours=2
    )
    assert not poll.is_error
    (posted,) = [b for b in server.sent("POST", f"/channels/{CHANNEL}/messages") if "poll" in b]
    assert posted["poll"]["duration"] == 2 and len(posted["poll"]["answers"]) == 2
    bad_poll = await tool.run(action="poll", channel=CHANNEL, question="?", answers=["one"])
    assert bad_poll.is_error and "2 to 10" in bad_poll.content
    missing = await tool.run(action="pin", channel=CHANNEL)
    assert missing.is_error and "needs message" in missing.content
    unknown = await tool.run(action="ban")
    assert unknown.is_error

    # Moderation is off by default, on by the setting, and the tool as a whole can be off.
    refused = await tool.run(action="delete_message", channel=CHANNEL, message="51")
    assert refused.is_error and "moderation" in refused.content
    assert not server.sent("DELETE", f"/channels/{CHANNEL}/messages/51")
    allowed = await DiscordTool(moderation=True).run(
        action="delete_message", channel=CHANNEL, message="51"
    )
    assert not allowed.is_error
    assert ("DELETE", f"/channels/{CHANNEL}/messages/51", None) in server.calls
    lifted = await DiscordTool(moderation=True).run(
        action="timeout_member", server=GUILD, user="999", minutes=0
    )
    assert "lifted" in lifted.content
    (patched,) = server.sent("PATCH", f"/guilds/{GUILD}/members/999")
    assert patched == {"communication_disabled_until": None}
    off = await DiscordTool(actions=False).run(action="servers")
    assert off.is_error and "actions" in off.content
    await adapter.stop()
    assert "adapter" not in RUNNING


# -- loop detection ----------------------------------------------------------------------


def test_the_tool_drops_the_id_it_was_given() -> None:
    """A poll or a thread posted twice is the same outcome, whatever id Discord gave it."""
    discord = DiscordTool()

    assert discord.outcome_key({}, "poll posted: message 111") == discord.outcome_key(
        {}, "poll posted: message 222"
    )
    assert discord.outcome_key(
        {}, "thread 9 created in 5: address thread:discord:5:9"
    ) == discord.outcome_key({}, "thread 10 created in 5: address thread:discord:5:10")
