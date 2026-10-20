---
name: perplexity
description: Perplexity as a web_search backend - its ranked results, or a written answer with its sources.
version: "1.0.0"
requires_ultron_sdk: ">=1.15,<2"
categories: [web, search]
contracts:
  searchers: [perplexity]
config_schema:
  api_key_env:
    type: str
    default: PERPLEXITY_API_KEY
    description: The variable the key is read from, in the environment or ~/.ultron/.env.
  mode:
    type: str
    default: search
    description: "search returns ranked pages from the Search API. agent returns a written answer with its sources, from the Agent API, billed by the token as well."
  search_type:
    type: str
    default: web
    description: "web, or fast for lower latency and cost."
  preset:
    type: str
    default: fast
    description: "In agent mode, the Agent API preset that writes the answer: fast is the old sonar, low the old sonar-reasoning-pro."
  country:
    type: str
    default: ""
    description: "A two-letter country code to localise results to, e.g. US or DE."
  language:
    type: str
    default: ""
    description: "A two-letter language code, e.g. en or de."
  timeout_seconds:
    type: float
    default: 0
    description: How long one search may take. Zero is 30 seconds in search mode and 90 in agent mode.
---

# perplexity

[Perplexity](https://www.perplexity.ai)'s API as a `web_search` backend, in one of two
modes:

- **`search`** (the default): the Search API - ranked pages with a title, a URL, a
  snippet and a date, the way Brave answers. Nothing is written by a model.
- **`agent`**: the Agent API - Perplexity searches, then writes an answer, and the model
  is given the answer, its cited URLs, and the pages behind it.

```
/plugins install perplexity
```

**Ready when a key is present**, and absent from `web_search` until then. Make one in
the API settings of your Perplexity account and store it as `PERPLEXITY_API_KEY`:
`ultron "store my perplexity api key"` puts it in `~/.ultron/.env`, never in
`auth-profiles.json`, which is for model vendors. It is read fresh on every search and
sent to `api.perplexity.ai` and nowhere else.

## What reaches Perplexity

Every filter `web_search` offers, in both modes:

| `web_search` | Perplexity |
|---|---|
| `count` | `max_results`, up to 20 |
| `freshness` | `search_recency_filter` |
| `date_after`, `date_before` | `search_after_date_filter`, `search_before_date_filter` |
| `domains` | `search_domain_filter`, twenty at most |

Perplexity's domain filter is an allow list or a deny list, never both. A search that
asks for some domains and excludes others keeps the domains and drops the exclusions,
which are mostly outside the allowed domains anyway.

## The answer mode

```jsonc
{ "plugins_settings": { "perplexity": { "mode": "agent" } } }
```

This is what Sonar was. Perplexity ended Sonar Chat Completions on 2026-09-27 and moved
it to the Agent API's presets, so the plugin speaks `/v1/agent` with the `fast` preset
by default - Perplexity's own replacement for `sonar`. `low` stands in for
`sonar-reasoning-pro`:

```jsonc
{ "plugins_settings": { "perplexity": { "mode": "agent", "preset": "low" } } }
```

Each request is sent with `store: false`, so Perplexity keeps none of them for later
retrieval. The answer is a vendor's prose, and reaches the model inside the same
untrusted envelope as the pages; only the cited URLs are kept, never their titles.

## With another searcher installed

The `search` mode sorts beside Brave (priority 50), and two at the same priority go in
name order; the `agent` mode sorts after them (70), because it costs more. To make
Perplexity the one, name it:

```jsonc
{ "web_searcher": "perplexity" }
```

## Settings

Under `plugins_settings.perplexity`:

```jsonc
{
  "mode": "search",          // search or agent
  "search_type": "web",      // web or fast
  "preset": "fast",          // agent mode only
  "country": "",             // e.g. "US"
  "language": "",            // e.g. "en"
  "timeout_seconds": 0       // 0: 30 s for search, 90 s for agent
}
```
