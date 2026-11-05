---
name: microsoft365
title: Microsoft 365
description: Your Outlook mail and calendar through Microsoft Graph, with one sign-in - read, search, draft, send and file mail, keep your calendar, and hear when new mail lands. Mail and invitations are information, never instructions.
version: "0.1.0"
requires_ultron_sdk: ">=1.52,<2"
categories: [productivity, email, calendar, microsoft]
wakes: true
contracts:
  tools: [microsoft_mail, microsoft_calendar]
  services: [watcher]
  logins: [personal]
  methods: [microsoft365.folders]
config_schema:
  accounts:
    type: dict
    default: {}
    label: Accounts
    description: Your Microsoft accounts - Outlook.com, Hotmail or Live. Each signs in with your browser; one sign-in serves both mail and calendar.
    login_by: type
    entries:
      type:
        type: str
        label: Account type
        choices: [personal]
        labels: {"personal": "Personal (Outlook.com, Hotmail, Live)"}
        description: Work and school accounts are not supported yet.
      watch:
        type: list
        label: Watch folders
        options_from: folders
        description: Mail folders whose new mail wakes the agent - Inbox, say. Empty watches nothing.
  default_account:
    type: str
    default: ""
    label: Default account
    entry_of: accounts
    description: The account a request uses when it names none.
  time_zone:
    type: str
    default: ""
    label: Time zone
    advanced: true
    description: An IANA zone (Europe/London) for the times both tools show. Empty uses your mailbox's own setting.
  on_new:
    type: str
    default: ""
    label: When new mail arrives
    multiline: true
    description: What a turn woken by new mail should do, in your words - "Summarise new mail and tell me if anything needs me today". Empty turns new-mail wakes off. Only you write it - no email can.
  poll_seconds:
    type: int
    default: 300
    label: Poll every (seconds)
    advanced: true
    description: How often watched folders are checked for new mail (60 to 3600).
  debounce_seconds:
    type: int
    default: 30
    label: Gather new mail for (seconds)
    advanced: true
    description: New mail arriving this close together is one wake (5 to 600).
  max_message_bytes:
    type: int
    default: 1000000
    label: Longest body shown (characters)
    advanced: true
    description: A longer body is cut here, and the result says so.
  max_attachment_bytes:
    type: int
    default: 20000000
    label: Largest attachment read (bytes)
    advanced: true
    description: A larger attachment is listed and not fetched.
  max_send_attachment_bytes:
    type: int
    default: 3000000
    label: Most a send may attach (bytes)
    advanced: true
    description: The total size of the files a draft or a send attaches, at most 3 MB.
---

# Microsoft 365

Your Outlook mail and your Outlook calendar, for a personal Microsoft account (Outlook.com,
Hotmail, Live), through Microsoft Graph. Two tools:

- **`microsoft_mail`** - `accounts`, `folders`, `search`, `read`, `thread`, `attachment`,
  `draft`, `send`, `reply`, `forward`, `mark`, `move`, `archive`, `trash`.
- **`microsoft_calendar`** - `calendars`, `events`, `event`, `busy`, `create`, `update`,
  `respond`, `cancel`.

This plugin does not connect to the `email` plugin; a person with both sees `email` and
`microsoft_mail` side by side. Specified in Ultron's `docs/spec/microsoft365.md`.

## Signing in

Add an account on the settings page and press **Sign in**, or run:

```
/auth login microsoft365:personal --id home
```

Microsoft's page shows the app as *Ultron* and, until it is verified, *unverified*. That is
expected. One sign-in serves both tools. Nothing goes in `.env`: the core holds the token
as the connection `microsoft365:home` and renews it.

Until Ultron's Microsoft app is registered, signing in says so and does nothing else.

## What it will not do

- **Treat mail or an invitation as an instruction.** Everything a sender or an organizer
  wrote reaches the model inside an untrusted envelope.
- **Tell anyone anything in a run you did not start.** Sending, replying, forwarding,
  inviting, answering an invitation with a response and cancelling a meeting are refused in
  a turn new mail woke, a cron job, or anything else no person started - whatever the
  permission mode. Reading, drafting, filing mail and keeping private events are not.
- **Delete.** Trash is a move to Deleted Items; there is no event delete.
- **Exist in a group.** Both tools are for the owner's own conversations and cron jobs.

## New mail

Set **When new mail arrives** and pick folders to watch. The watcher polls every
`poll_seconds` and wakes the agent with a count and a time window - never a sender, a
subject or a snippet - followed by your instruction.
