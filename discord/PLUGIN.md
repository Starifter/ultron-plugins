---
name: discord
description: Discord as a channel - DMs, server channels and threads through a bot. Needs DISCORD_BOT_TOKEN in ~/.ultron/.env.
categories: [channel, messaging]
logo: logo.svg
version: "1.0.0"
requires_ultron_sdk: ">=1.21,<2"
requires_env: [DISCORD_BOT_TOKEN]
contracts:
  channels: [discord]
  tools: [discord]
config_schema:
  slash_commands:
    type: bool
    default: true
    description: Register Ultron's slash commands with Discord as native application commands at connect.
  actions:
    type: bool
    default: true
    description: Offer the `discord` tool - servers, channels, history, pins, threads, polls - to the model.
  moderation:
    type: bool
    default: false
    description: Let the `discord` tool delete messages and time members out.
---

# discord

A Discord bot as a channel (`docs/spec/sessions.md` §8, `channels.md` C8): DMs,
server channels and threads. It holds one Gateway websocket session per account
from inside the gateway process - heartbeats, identify, resume - and answers over
the REST API, the model's markdown sent as written because Discord draws
markdown itself, split at Discord's 2000-character limit. Every REST call goes
through `ultron.sdk.web`, so the address policy the core applies to `web_fetch`
applies here too; the websocket's address is checked under the same policy
before it is opened. No Discord library is needed.

**Installed from the official marketplace.** Ultron shipped this plugin until
it moved here; `/plugins install discord` copies it into `~/.ultron/plugins/` and
enables it, and it loads on the next session.

**Ready when a token is present**, and not started until then: with no
`DISCORD_BOT_TOKEN` this plugin loads, registers, and reports itself not ready,
so an install connects to nothing. Make an application in the
[Developer Portal](https://discord.com/developers/applications), add a bot,
switch **Message Content Intent** on under *Privileged Gateway Intents* (without
it every server message arrives empty), invite it to your server with the `bot`
scope, then `ultron "store my discord bot token"` - it goes to `~/.ultron/.env`
and never into `config.json`. A token Discord refuses, or an intent the portal
has not granted, stops that account and says so in `/gateway`'s presence.

**Who may talk to it is the core's decision, not this plugin's.** Put your own
Discord user id on `channels_owners` as `discord:<id>` (copy it with Developer
Mode on); a DM from anyone else is dropped and audited (R8.4). In a server the
bot runs a turn only when addressed - an `@mention` or a reply to one of its
messages - and records the rest (R8.6).

**Ids are Discord's snowflakes**, digits already in the key alphabet: a DM is
`dm:discord:<user id>`, a server channel `group:discord:<channel id>`, a thread
`thread:discord:<parent channel id>:<thread id>`. A reply to a DM goes to the DM
channel the message came in on, or opens one if the bot has not heard from that
person since it started.

**Asking works, with buttons.** The adapter declares `can_ask`, so `ask_user` on a
Discord-only session sends the question to the chat - options as buttons, up to
twenty-five - and takes the owner's next message or press there as the answer,
within `channels_ask_timeout_seconds`. A pressed set of buttons is taken off the
message.

**Replies appear as they are written.** The bot shows *typing* while a run is
live and sends the reply as a draft it edits in place, then finalises
(`channels.md` C1). In a server a reply references the message that asked,
without pinging its author.

**More than one bot.** `DISCORD_BOT_TOKEN_<NAME>` beside `DISCORD_BOT_TOKEN` is
a second account called `<name>`, connected alongside; a reply goes out through
the bot the message came in on. Owners are `discord:<user id>` on any of them.

**Pictures and files.** An attached image is downloaded for the model when it
fits `images_max_bytes`; a voice note, clip or document when its kind is on and
it fits (`media.md` §12); anything else is named and not fetched.

**Reactions, edits, deletions, the "seen" mark.** A reaction somebody puts on or
takes off a message, a message they edit and one they delete arrive for the
transcript; with `channels_ack_reaction` set the bot reacts to your message while
it works and settles it when the reply lands. Taking the bot's reaction off takes
every reaction of its own off that message, because Discord names them by emoji
and the core does not.

**Forum channels take posts.** A message sent to a forum or media channel starts a
thread named by its first line and lands inside it; the reply's edits follow it
there. A post somebody else starts arrives as a thread under the forum, like any
thread.

**Slash commands are native.** At connect the bot registers Ultron's commands as
Discord application commands - `/status`, `/new`, `/model` and the rest, each with
one `args` field - so the picker shows them. A command a person picks arrives as
the same `/name args` text a typed one is, and the core runs it under the same
rule: an owner's command runs, anyone else's is text. `slash_commands: false` in
`plugins_settings.discord` keeps the menu empty; typed commands still work.

**Per-room policy.** `channels_policy.discord.rooms.<channel id>` may set
`group_trigger` (`mention`, `always`, `never`) and `enabled` for one server channel;
a thread follows its parent's row (`channels.md` C3.2).

**The `discord` tool.** Beyond replying, the model can list the servers the bot is in
and a server's channels, describe a channel, read its recent history, look a member
or the roles up, pin and unpin, start a thread, and post a poll. What comes back is
other people's text and reaches the model enveloped. `delete_message` and
`timeout_member` exist only with `moderation: true`; `actions: false` withholds the
tool's actions altogether. Every call is a tool call in the trail.

**Always pushed.** Discord delivers events over the websocket it holds; there is
no polling and no webhook to open, so `channels_webhook_*` does not apply here.

The token is sent to `discord.com` and its gateway and nowhere else, and never
appears in an error: anything this plugin raises names it through `fingerprint`.
