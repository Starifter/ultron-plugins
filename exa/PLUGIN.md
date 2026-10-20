---
name: exa
description: Exa as a web_search backend - search by meaning, with the passages that match.
version: "1.0.0"
requires_ultron_sdk: ">=1.15,<2"
categories: [web, search]
logo: logo.svg
contracts:
  searchers: [exa]
config_schema:
  api_key_env:
    type: str
    default: EXA_API_KEY
    description: The variable the key is read from, in the environment or ~/.ultron/.env.
  base_url:
    type: str
    default: https://api.exa.ai
    description: "Where Exa is, for a compatible proxy. A bare host gets https://, and /search is added."
  search_type:
    type: str
    default: auto
    description: "auto, lite (fastest), deep-lite or deep (multi-step, with a written synthesis). Exa's older names - neural, fast, instant, deep-reasoning - still work."
  category:
    type: str
    default: ""
    description: "A kind of page to search: company, publication, news, personal site, financial report or people. Empty searches everything."
  country:
    type: str
    default: ""
    description: "A two-letter country code the results are for, e.g. US or DE."
  highlights:
    type: bool
    default: true
    description: The passages of each page that match the query - what each row shows.
  summary:
    type: bool
    default: false
    description: A summary of each page Exa writes, shown when there are no highlights.
  text_max_characters:
    type: int
    default: 0
    description: "Read each page's text, up to this many characters. Zero reads none - web_fetch reads a page in full."
  timeout_seconds:
    type: float
    default: 30
    description: How long one search may take.
---

# exa

[Exa](https://exa.ai)'s search API, OpenClaw's `exa` provider ported: a search engine that
finds pages by what they mean rather than which words they share, and returns the
passages of each page that match - so a row is the part of the page that answers.

```
/plugins install exa
```

**Ready when a key is present**, and absent from `web_search` until then. Make a key in
the Exa dashboard and store it as `EXA_API_KEY`: `ultron "store my exa api key"` puts it
in `~/.ultron/.env`, never in `auth-profiles.json`, which is for model vendors. It is read
fresh on every search and sent to `base_url` and nowhere else.

## What reaches Exa

Every filter `web_search` offers:

| `web_search` | Exa |
|---|---|
| `count` | `numResults`, up to 100 |
| `freshness` | `startPublishedDate`, a day, week, month or year back - OpenClaw's mapping |
| `date_after`, `date_before` | `startPublishedDate`, `endPublishedDate` |
| `domains` | `includeDomains`, and `-domain` as `excludeDomains` |

Where OpenClaw's tool lets the model pick a search `type` and the `contents` per call,
here they are settings: `web_search` has one schema for every searcher, and a property
only one of them reads would cost tokens on every request for all of them.

## What each row shows

The highlights by default, as in OpenClaw; with them off, Exa's summary of the page;
with neither, its text. Turn on what you want read:

```jsonc
{ "plugins_settings": { "exa": { "highlights": false, "summary": true } } }
```

A **deep** search (`search_type: deep` or `deep-lite`) also writes an answer from what it
found; that is shown above the rows, cited by them. It is a vendor's prose, and like the
rows it reaches the model inside the untrusted envelope.

## With another searcher installed

Exa sorts beside Brave (priority 50), and two at the same priority go in name order. To
make Exa the one, name it:

```jsonc
{ "web_searcher": "exa" }
```

## Settings

Under `plugins_settings.exa`:

```jsonc
{
  "search_type": "auto",        // auto, lite, deep-lite, deep
  "category": "",               // company, publication, news, ...
  "country": "",                // e.g. "US"
  "highlights": true,
  "summary": false,
  "text_max_characters": 0,
  "base_url": "https://api.exa.ai",
  "timeout_seconds": 30
}
```

A `base_url` with a username or password in it is refused: a credential is not a
setting.
