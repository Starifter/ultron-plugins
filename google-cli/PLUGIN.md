---
name: google-cli
description: Google Workspace - Calendar, Gmail, Drive, Docs, Sheets and more - through the gog CLI; reads run read-only, every change is a card you approve.
version: "1.0.0"
requires_ultron_sdk: ">=1.41,<2"
categories: [productivity, google]
contracts:
  tools: [google]
config_schema:
  executable:
    type: str
    default: gog
    description: The gog binary - a name on PATH or a full path to gog.exe.
  account:
    type: str
    default: ""
    description: The Google account (email or gog alias) every call uses. Empty is gog's own default (GOG_ACCOUNT, or its single signed-in account).
  readonly:
    type: bool
    default: false
    description: Refuse every write. Ultron can read your account and never change it.
  gmail_no_send:
    type: bool
    default: false
    description: Pass gog's --gmail-no-send, so no write can send an email even when you approve the card.
  timeout_seconds:
    type: int
    default: 120
    description: How long one gog call may run before it is killed.
  max_output_chars:
    type: int
    default: 60000
    description: The most of gog's output the model is handed per call; the rest is cut with a note.
---

# google-cli

Google Workspace for Ultron, the way OpenClaw does it: through
[`gog`](https://github.com/openclaw/gogcli), one command-line client for Gmail, Calendar,
Drive, Docs, Sheets, Slides, Contacts, Tasks, Forms, Meet, Chat, YouTube and the rest. The
plugin adds one tool, `google`. The model gives it the command after `gog`, the plugin runs
it, and the model reads the JSON that comes back.

```
/plugins install google-cli
```

OpenClaw gives its model the same CLI as a skill and lets it run `gog` from the shell.
Here it is a tool instead. That means the output gets marked as untrusted, the account is
fixed by you, and a write is a card you answer. None of that is possible when the model is
just typing shell commands.

## Setup

These steps are yours to do at a terminal, once. The tool never signs in for you.

1. **Install gog.**
   - Windows: download `gogcli_<version>_windows_amd64.zip` from the
     [releases](https://github.com/openclaw/gogcli/releases) and put `gog.exe` on `PATH`.
     Alternatively, set `plugins_settings.google-cli.executable` to its full path.
   - macOS/Linux: `brew install openclaw/tap/gogcli`.
   - Anywhere with Go: `go install github.com/openclaw/gogcli/cmd/gog@latest`.
2. **Make a Google OAuth client.**
   1. In [Google Cloud](https://console.cloud.google.com/auth/clients), create a project.
   2. Enable the APIs you want, such as Calendar, Gmail and Drive.
   3. Set up the consent screen, and add yourself as a test user.
   4. Create a **Desktop app** OAuth client and download its JSON.
3. **Sign gog in:**
   ```
   gog auth credentials set C:\path\to\client_secret.json
   gog auth add you@gmail.com --services calendar,gmail,drive,docs,sheets,contacts,tasks
   gog auth doctor --check
   ```
   Authorize only the services you want Ultron to see. `gog auth add --readonly` asks Google
   for read-only scopes where they exist, and that is a stronger line than any setting here.
4. **Optional:** set `plugins_settings.google-cli.account` to that email. This matters if you have
   more than one account signed in.

The token stays in gog's store, which is your OS keyring by default. It never reaches
Ultron's config, its audit trail or the model. If the consent screen is left in testing,
Google expires the token after about a week. When that happens, a call says *auth
required*, and you run `gog auth add` again.

## What the model can and cannot do

- **Reads are the default.** They run with gog's own `--readonly`, which refuses any
  mutating Google API request before it is sent. This is decided by gog at request time,
  not by a list of command names, so a new command that writes is still refused. A read
  needs no card.
- **A write is `write: true`, and every one is a card.** This covers creating, updating
  or deleting an event, sending, labelling or archiving mail, and uploading, sharing or
  editing a file. The card shows the whole command, and you answer Allow or Deny. It goes
  through the session's permission gate like any gated tool, so in `yolo` mode no card
  is shown and the write runs. Use `readonly` if you want a line that `yolo` cannot
  cross. If a model calls a write without the flag, gog
  refuses it, and the result tells the model to ask again with `write: true`.
- **The plugin owns the safety line.** The model cannot pass:
  - `--account`, `--access-token`, `--client`, or `--home`;
  - `--readonly`, `--enable-commands`, `--disable-commands`, or `--gmail-no-send`;
  - the output flags.

  It cannot run `auth`, `login`, `logout`, `config`, `mcp`, `completion` or `update`
  either. `status` stays allowed, so a failed call can find out why.
- **The owner's sessions only.** The tool is `trusted_only`: it is removed from groups,
  threads, cron runs, webhook sessions and a stranger's DM, and no allow list adds it back.
  The account is yours, so a request has to come from you.
- **Everything gog prints is untrusted.** Event titles, invite descriptions, email bodies
  and documents all arrive inside an `ULTRON_UNTRUSTED` envelope. Anyone can send you an
  invite. The exit code (gog's documented codes: `auth_required`, `not_found`,
  `rate_limited`, ...) and the plugin's own hints stay outside the envelope.

## Settings

Under `plugins_settings.google-cli` in `config.json`; `/plugins google-cli` shows them.

| Setting | Default | What it does |
|---|---|---|
| `executable` | `gog` | The binary, on `PATH` or a full path. |
| `account` | `""` | The account every call uses. |
| `readonly` | `false` | Refuse every write, so no card is ever shown. |
| `gmail_no_send` | `false` | gog's `--gmail-no-send`: no email is sent even on an approved write. |
| `timeout_seconds` | `120` | How long one call may run. |
| `max_output_chars` | `60000` | How much output the model is handed per call. |

## Known edges

- **gog runs in the workspace.** A read that downloads a file, such as `drive download` or
  `gmail attachment`, writes it there by default. `--out` can name a path elsewhere,
  because gog's read-only mode guards Google and not your disk. Uploading a local file is a
  write, and so is a card.
- **The environment is scrubbed.** gog runs on Ultron's scrubbed environment, so
  `GOG_KEYRING_PASSWORD` for gog's encrypted-file backend does not reach it. Use the OS
  keyring, which is gog's default.
- **Ultron does not check gog's version.** The plugin relies on `--readonly`, `--no-input`,
  `--json` and the exit codes, which gog has documented as its automation contract.
