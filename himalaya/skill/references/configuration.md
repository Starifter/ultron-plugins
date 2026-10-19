# Himalaya 2.x configuration

The person writes this, or runs the bare `himalaya` wizard and keeps what it prints. Do not
write a password into it: every `password` / `token` field takes a `.command` that prints
the secret from a password manager, and `.raw` is for testing only. Native keyring support
was removed in 2.x - a keyring is reached through a CLI such as `secret-tool` or `pass`.

The file is the first that exists of `$XDG_CONFIG_HOME/himalaya/config.toml`,
`~/.config/himalaya/config.toml` and `~/.himalayarc`; `-c <path>` overrides it. A documented
sample is `config.sample.toml` in the himalaya repository.

## Generic IMAP + SMTP

```toml
[accounts.personal]

imap.server = "imaps://imap.example.com"
imap.sasl.plain.username = "me@example.com"
imap.sasl.plain.password.command = "pass show mail/example"

smtp.server = "smtps://smtp.example.com"
smtp.sasl.plain.username = "me@example.com"
smtp.sasl.plain.password.command = "pass show mail/example"
```

`imaps://` and `smtps://` are implicit TLS. A server on a STARTTLS port takes `imap://` or
`smtp://` with `starttls = true`:

```toml
smtp.server = "smtp://smtp.example.com:587"
smtp.starttls = true
```

## Gmail

An app password (it needs 2-step verification), and aliases for the `[Gmail]/` mailboxes:

```toml
[accounts.gmail]

imap.server = "imaps://imap.gmail.com:993"
imap.sasl.plain.username = "me@gmail.com"
imap.sasl.plain.password.command = "pass show gmail"

smtp.server = "smtps://smtp.gmail.com:465"
smtp.sasl.plain.username = "me@gmail.com"
smtp.sasl.plain.password.command = "pass show gmail"

mailbox.alias.sent = "[Gmail]/Sent Mail"
mailbox.alias.drafts = "[Gmail]/Drafts"
mailbox.alias.trash = "[Gmail]/Trash"
mailbox.alias.archive = "[Gmail]/All Mail"
```

## iCloud

An app-specific password. The IMAP username is the bare name (`johnappleseed`); the SMTP one
is the full address.

```toml
[accounts.icloud]

imap.server = "imaps://imap.mail.me.com:993"
imap.sasl.plain.username = "johnappleseed"
imap.sasl.plain.password.command = "pass show icloud"

smtp.server = "smtp://smtp.mail.me.com:587"
smtp.starttls = true
smtp.sasl.plain.username = "johnappleseed@icloud.com"
smtp.sasl.plain.password.command = "pass show icloud"

mailbox.alias.sent = "Sent Messages"
```

## Outlook / Microsoft 365

Basic authentication is retired: an OAuth 2.0 token from a helper such as `ortie`, through
`xoauth2`.

```toml
[accounts.outlook]

imap.server = "imaps://outlook.office365.com:993"
imap.sasl.xoauth2.username = "me@outlook.com"
imap.sasl.xoauth2.token.command = ["ortie", "token", "show", "-a", "outlook"]

smtp.server = "smtp://smtp-mail.outlook.com:587"
smtp.starttls = true
smtp.sasl.xoauth2.username = "me@outlook.com"
smtp.sasl.xoauth2.token.command = ["ortie", "token", "show", "-a", "outlook"]
```

## JMAP (Fastmail and others)

One block replaces `imap` and `smtp`:

```toml
jmap.server = "https://api.fastmail.com/jmap/session"
jmap.auth.bearer.token.command = "pass show fastmail"
```

## Proton Mail

Through Proton Bridge, which serves IMAP and SMTP locally; the password is the one the
Bridge generated.

```toml
[accounts.proton]

imap.server = "imap://127.0.0.1:1143"
imap.sasl.plain.username = "me@proton.me"
imap.sasl.plain.password.command = "pass show proton-bridge"

smtp.server = "smtp://127.0.0.1:1025"
smtp.sasl.plain.username = "me@proton.me"
smtp.sasl.plain.password.command = "pass show proton-bridge"
```

## Several accounts

Each is its own `[accounts.<name>]` table; pick one per command with `-a/--account <name>`.
`default = true` in one of them is the account used when `-a` is not passed.
