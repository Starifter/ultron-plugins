---
name: searxng
description: Your own SearXNG as a web_search backend - no key, no bill, no tracking.
version: "1.0.0"
requires_ultron_sdk: ">=1.15,<2"
categories: [web, search, local]
contracts:
  searchers: [searxng]
config_schema:
  base_url:
    type: str
    default: ""
    description: "Your SearXNG, e.g. http://localhost:8080 or https://search.example.com/searxng. Empty reads SEARXNG_BASE_URL."
  categories:
    type: str
    default: ""
    description: "Comma-separated SearXNG categories, e.g. general, news or science. Empty is the instance's default."
  language:
    type: str
    default: ""
    description: "A language for results, e.g. en, de or fr. Empty is the instance's default."
  timeout_seconds:
    type: float
    default: 20
    description: How long one search may take.
---

# searxng

[SearXNG](https://docs.searxng.org/), a meta-search engine you run yourself, OpenClaw's
`searxng` provider ported. It asks the engines you have enabled and returns what they
found; nothing is sent to anybody but your instance and the engines it asks, and there is
no key and no bill.

```
/plugins install searxng
```

## Point it at your instance

Run one - the [SearXNG Docker image](https://docs.searxng.org/admin/installation-docker.html)
is the short way - and **turn on its JSON output**, which is off by default. In the
instance's `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

Then tell the plugin where it is, either way:

```jsonc
{ "plugins_settings": { "searxng": { "base_url": "http://localhost:8080" } } }
```

```
SEARXNG_BASE_URL=http://localhost:8080        # in ~/.ultron/.env
```

The setting wins. The plugin is **ready when either is set**, and absent from
`web_search` until then. `/search` is added to the path unless it is already there, so a
SearXNG under a path prefix works as it is written.

**`http://` only on your own machine or network.** As in OpenClaw: an instance reached
over plain HTTP must resolve, every address of it, to loopback or a private range,
because a search sent in clear text across the internet is read by anybody on the path.
A public instance needs `https://`. A base URL with a username or password in it is
refused - a credential is not a setting, and a URL is not where one goes.

## What reaches SearXNG

| `web_search` | SearXNG |
|---|---|
| `count` | the first `count` results; SearXNG has no count of its own |
| `freshness` | `time_range` |
| `date_after` | the smallest `time_range` that reaches back to it; nothing past a year |
| `date_before` | nothing - SearXNG cannot bound a search's end |
| `domains` | `site:` and `-site:` in the query, which most engines read |

A search in categories that finds nothing is asked again in `general`, as OpenClaw does,
rather than reported empty.

## With another searcher installed

SearXNG sorts first (priority 40), before the searchers that charge per search, because
an instance you run costs nothing. To prefer another, name it:

```jsonc
{ "web_searcher": "brave" }
```

## Settings

Under `plugins_settings.searxng`:

```jsonc
{
  "base_url": "http://localhost:8080",
  "categories": "",          // e.g. "general,science"
  "language": "",            // e.g. "en"
  "timeout_seconds": 20
}
```

## When it says 403

SearXNG answers `403 Forbidden` to a JSON request when `json` is not in
`search.formats`. Add it (above) and restart the instance.
