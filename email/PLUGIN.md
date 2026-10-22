---
name: email
description: Your email over IMAP and SMTP - read, search, draft, send and file mail, and hear when new mail lands. Mail is information, never instructions.
version: "1.2.0"
requires_ultron_sdk: ">=1.46,<2"
categories: [productivity, email]
wakes: true
python_dependencies:
  - "aioimaplib>=2"
  - "aiosmtplib>=3"
contracts:
  tools: [email]
  services: [watcher]
  logins: [gmail, outlook]
  methods: [email.folders]
secrets:
  ULTRON_EMAIL_GMAIL_CLIENT_SECRET: The secret of your own Google OAuth client (a Desktop app), for signing in to Gmail instead of an app password. Its id is the Gmail client id under Advanced.
config_schema:
  accounts:
    type: dict
    default: {}
    description: Your mailboxes. A Gmail or Outlook account signs in with your browser; any other gives its servers and an app password.
    login_by: provider
    secret: ULTRON_EMAIL_PASSWORD_{NAME}
    secret_label: App password
    entries:
      provider:
        type: str
        label: Provider
        choices: [gmail, outlook, ""]
        labels: {"gmail": "Gmail", "outlook": "Outlook", "": "Other (IMAP)"}
        description: Gmail and Outlook fill in their servers and sign in with your browser. Other is any IMAP server, with an app password.
      address:
        type: str
        label: Address
        required: true
        format: email
        description: The address mail is sent from.
      imap_host:
        type: str
        label: IMAP server
        when: {"provider": [""]}
        description: Where mail is read from - imap.fastmail.com, say.
      smtp_host:
        type: str
        label: SMTP server
        when: {"provider": [""]}
        description: Where mail is sent through - smtp.fastmail.com, say.
      watch:
        type: list
        label: Watch folders
        options_from: folders
        description: Folders whose new mail wakes the agent. Empty watches nothing.
      name:
        type: str
        label: Display name
        advanced: true
        description: The name on mail you send.
      username:
        type: str
        label: Username
        advanced: true
        description: The login, when it is not the address.
      imap_port:
        type: int
        label: IMAP port
        advanced: true
        when: {"provider": [""]}
        description: IMAP over TLS; 993 when empty.
      smtp_port:
        type: int
        label: SMTP port
        advanced: true
        when: {"provider": [""]}
        description: 465 (TLS) or 587 (STARTTLS); 465 when empty.
      sent_folder:
        type: str
        label: Sent folder
        advanced: true
        description: Where a sent copy is filed. Empty finds it; Gmail and Outlook keep their own.
      drafts_folder:
        type: str
        label: Drafts folder
        advanced: true
        description: Empty finds it by the server's folder roles.
      trash_folder:
        type: str
        label: Trash folder
        advanced: true
        description: Empty finds it by the server's folder roles.
      archive_folder:
        type: str
        label: Archive folder
        advanced: true
        description: Empty finds it by the server's folder roles.
  default_account:
    type: str
    default: ""
    label: Default account
    entry_of: accounts
    description: The account a request uses when it names none.
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
    description: How often a server without IMAP IDLE is checked for new mail (60 to 3600).
  debounce_seconds:
    type: int
    default: 30
    label: Gather new mail for (seconds)
    advanced: true
    description: New mail arriving this close together is one wake (5 to 600).
  max_message_bytes:
    type: int
    default: 1000000
    label: Longest message body
    advanced: true
    description: A message body is cut at this many characters, and the result says so.
  max_attachment_bytes:
    type: int
    default: 20000000
    label: Largest attachment read
    advanced: true
    description: Larger attachments are listed and never fetched.
  max_send_attachment_bytes:
    type: int
    default: 20000000
    label: Most attached to one send
    advanced: true
    description: The most a send may attach, in total.
  gmail_client_id:
    type: str
    default: ""
    label: Gmail client id
    advanced: true
    description: The id of your own Google OAuth client (a Desktop app), for signing in to Gmail. Its secret is under Secrets.
  outlook_tenant:
    type: str
    default: common
    label: Outlook tenant
    advanced: true
    description: Which Microsoft accounts may sign in - common (personal and work), consumers, organizations, or one organisation's tenant id or domain.
  outlook_client_id:
    type: str
    default: ""
    label: Outlook client id
    advanced: true
    description: A Microsoft client id to sign in with, for an organisation that allows only apps it registered itself.
---

# email

Your mail for Ultron, over plain IMAP and SMTP, so it works with any provider: Fastmail,
iCloud, your own server with an app password, and Gmail and Outlook with an OAuth sign-in
(see [Gmail and Outlook](#gmail-and-outlook)). One tool, `email`,
reads, searches, drafts, sends and files mail across as many accounts as you configure.
One background service watches the folders you choose and tells the agent when new mail
lands.

```
/plugins install email
```

Installed from the official marketplace, its two libraries (`aioimaplib`, `aiosmtplib`) are
installed with it, into a folder of its own under `~/.ultron/plugin-libs/` - never into the
environment Ultron runs in. Installed from anywhere else, run
`uv pip install aioimaplib aiosmtplib` in Ultron's environment yourself.

## Mail is information, never instructions

Anyone can put words in front of your agent for the price of your address, so this
plugin treats mail the way `web_fetch` treats a web page:

- **Everything a sender wrote is enveloped.** Headers, body, attachment names and their
  contents reach the model inside an untrusted envelope, from every sender, your own
  address included. A `From:` line is something anybody can write.
- **No email is ever a prompt.** Mail never becomes a message to the agent, a turn, or a
  rule. You cannot email your agent instructions, and neither can anyone else.
- **New mail is a notification.** The wake the agent gets says only the account, the
  folder, how many arrived and their numbers, then your own `on_new` instruction. The
  agent reads the mail through the tool, enveloped.
- **No person, no send.** A turn that new mail started, a cron job or the heartbeat can
  read, search, draft and file, and cannot send, reply or forward. Sending happens only
  in a turn you started.
- **Nothing is deleted.** `trash` moves mail to your Trash folder; the plugin never
  expunges.

The tool is the owner's alone: it is not offered in a group chat, a thread, a cron run or
a stranger's DM.

## Setup

The easiest way is the web UI: open **Plugins**, then **email**, then **Settings**. Add an
account by name, fill in its fields and save. A Gmail or Outlook account then shows a
**Sign in** button; any other shows a field for its password, which goes to
`~/.ultron/.env` and never to config. The steps below do the same by hand.

1. Make an app password at your provider. Most refuse a normal password over IMAP; iCloud
   and Fastmail both have one. For Gmail and Outlook, sign in instead (next section).
2. Add the account in `~/.ultron/config.json`:

   ```json
   { "plugins_settings": { "email": {
       "accounts": {
         "personal": {
           "address": "me@fastmail.com",
           "imap_host": "imap.fastmail.com",
           "smtp_host": "smtp.fastmail.com",
           "watch": ["INBOX"]
         }
       },
       "on_new": "Summarise new mail and tell me if anything needs me today."
   } } }
   ```

3. Store the password with the `secret` tool (ask the agent to "store my email password"),
   or put it in `~/.ultron/.env` yourself:

   ```
   ULTRON_EMAIL_PASSWORD_PERSONAL=<the app password>
   ```

4. Start a new session. `email accounts` lists what the plugin can use.

| Account key | Default | Meaning |
|---|---|---|
| `address` | required | The address mail is sent from. |
| `name` | `""` | The display name on sent mail. |
| `username` | `address` | The login, when it differs from the address. |
| `imap_host`, `imap_port` | required, `993` | IMAP over TLS. |
| `smtp_host`, `smtp_port` | required, `465` | `465` is TLS, `587` is STARTTLS. There is no plaintext mode. |
| `watch` | `[]` | Folders whose new mail wakes the agent. Empty watches nothing. |
| `sent_folder` | `auto` | Where a sent copy is filed. `auto` finds your Sent folder, and files nothing for Gmail and Outlook, which keep their own copy. `""` never files one. |
| `drafts_folder`, `trash_folder`, `archive_folder` | `auto` | Found by the server's folder roles, else by name. |
| `provider` | `""` | `gmail` or `outlook`: fills in the hosts and ports and signs in with OAuth instead of a password. |

## Gmail and Outlook

Microsoft no longer takes a password over IMAP or SMTP, and Gmail takes one only as an app
password. With `provider` set, an account signs in with OAuth instead, over the same IMAP
and SMTP. The token is kept by Ultron in `~/.ultron/connections.json` and renewed on its own;
the plugin never stores it.

```json
{ "plugins_settings": { "email": {
    "accounts": {
      "work": { "provider": "outlook", "address": "me@contoso.com", "watch": ["INBOX"] },
      "personal": { "provider": "gmail", "address": "me@gmail.com" }
    }
} } }
```

**Outlook** (Outlook.com and Microsoft 365). Ultron's own Microsoft app is not registered yet,
so for now register one yourself in Microsoft Entra: a public client, platform *Mobile and
desktop*, redirect `http://localhost`. Put its id in `outlook_client_id`, then:

```
ultron auth login email:outlook --id work
```

`outlook_tenant` (default `common`) pins sign-in to one organisation when yours requires it.
If sending fails while reading works, your Microsoft 365 administrator has probably turned
SMTP AUTH off for your mailbox.

**Gmail.** Google lets an app read Gmail over IMAP only after a paid review, so you use a
client of your own:

1. In Google Cloud, create a project and enable the Gmail API.
2. Set up the OAuth consent screen. Choose *Internal* for a Workspace domain. For a personal
   account choose *External* and publish it **In production**, because a client left in
   *Testing* has its sign-ins expire after seven days. Google warns that the app is
   unverified; it is your own app.
3. Create an OAuth client of type *Desktop app*. Put its id in `gmail_client_id` and its
   secret in `~/.ultron/.env` as `ULTRON_EMAIL_GMAIL_CLIENT_SECRET` - both are on the
   plugin's Settings tab.
4. Start a new session, then sign in:

   ```
   ultron auth login email:gmail --id personal
   ```

The **Sign in** button on the plugin's Settings tab runs the same thing, and so does typing
`/auth login email:gmail --id personal` in a conversation. The `--id` is the account's
name, and the sign-in must be for that account's address.
`email accounts` shows whether each account is signed in. If a sign-in is revoked, that
account's new-mail watcher stops and starts again by itself once you sign in again.

## What the agent can do

| Action | Does | Needs your OK |
|---|---|---|
| `accounts`, `folders`, `search`, `read`, `thread`, `attachment` | Read | no |
| `draft` | Save a draft to Drafts | no |
| `send`, `reply`, `forward` | Send mail. Only in a turn you started. | as your permission mode says |
| `mark`, `move`, `archive`, `trash` | File mail | as your permission mode says |

Sends and changes are decided by your permission mode: `yolo` runs them, `ask` shows a card
naming the account, every recipient and the subject, `auto` hands them to the reviewer.
Attachments are read through Ultron's media readers, so a PDF in mail is read the way a
PDF in chat is. Nothing is saved to your workspace.

## New mail

With `on_new` set and a `watch` list, the plugin keeps a connection to each watched folder,
using IMAP IDLE where the server has it and checking every `poll_seconds` where it does not.
New unread mail is gathered for `debounce_seconds`, then the agent's main session gets one
turn:

```
[woken by plugin email]
New mail: personal INBOX 2 (uids 902, 903, uidvalidity 1714).
The person asked: Summarise new mail and tell me if anything needs me today.
```

Turning it on announces nothing old: the first start records where your mailbox is. Mail
that arrived while Ultron was off is one wake when it comes back. A wake the agent could
not take (busy for five minutes) keeps its mail for the next one. `plugins_no_wake: ["email"]`
stops the wakes without uninstalling anything.

There are no rules on what a message says. Sorting by sender or subject is a stranger
choosing what your agent does. Use your mail server's filters to sort mail into a folder,
and watch that folder.

The last message number seen per folder is kept in `<workspace>/.ultron/email.json`:
numbers and folder names, never mail.
