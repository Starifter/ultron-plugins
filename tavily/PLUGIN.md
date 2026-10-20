---
name: tavily
description: Tavily Search as a web_search backend - results built for agents, an answer if you want one.
version: "1.0.0"
requires_ultron_sdk: ">=1.15,<2"
categories: [web, search]
contracts:
  searchers: [tavily]
config_schema:
  api_key_env:
    type: str
    default: TAVILY_API_KEY
    description: The variable the key is read from, in the environment or ~/.ultron/.env.
  search_depth:
    type: str
    default: basic
    description: "basic, fast or ultra-fast cost one credit a search; advanced costs two and reads deeper."
  topic:
    type: str
    default: general
    description: general, news or finance. news dates every row.
  include_answer:
    type: str
    default: ""
    description: "basic or advanced puts Tavily's own written answer above the results. Empty is results only."
  country:
    type: str
    default: ""
    description: "A country to favour, in Tavily's words - united states, germany. Applies only to the general topic."
  language:
    type: str
    default: ""
    description: "A language to favour, e.g. en or de. Empty is Tavily's default."
  timeout_seconds:
    type: float
    default: 30
    description: How long one search may take.
---

# tavily

[Tavily](https://tavily.com)'s Search API, a search engine built to be read by an agent:
each row is the passage of the page that answers the query, not the page's meta
description.

```
/plugins install tavily
```

**Ready when a key is present**, and absent from `web_search` until then. Get a key at
[app.tavily.com](https://app.tavily.com) - the free plan has a monthly allowance - and
store it as `TAVILY_API_KEY`: `ultron "store my tavily api key"` puts it in
`~/.ultron/.env`, never in `auth-profiles.json`, which is for model vendors. It is read
fresh on every search, so a key stored mid-session is used on the next call, and it is
sent to `api.tavily.com` and nowhere else.

## What reaches Tavily

Every filter `web_search` offers:

| `web_search` | Tavily |
|---|---|
| `count` | `max_results`, up to 20 |
| `freshness` | `time_range` |
| `date_after`, `date_before` | `start_date`, `end_date` - either end alone is fine |
| `domains` | `include_domains`, and `-domain` as `exclude_domains` |

Rows carry their publication date where Tavily can find one.

## An answer, if you want one

Off by default: the model reads what the pages say, as it does from Brave. Turn it on
and Tavily writes a short answer from the same results, shown above them:

```jsonc
{ "plugins_settings": { "tavily": { "include_answer": "basic" } } }
```

`advanced` writes a longer one. Either way it is a vendor's prose, and reaches the model
inside the same untrusted envelope as the rows.

## With another searcher installed

Tavily sorts beside Brave (priority 50), and two at the same priority go in name order,
so with both installed and nothing named, Brave answers first and Tavily is tried if it
fails. To make Tavily the one, name it:

```jsonc
{ "web_searcher": "tavily" }
```

## Settings

Under `plugins_settings.tavily`:

```jsonc
{
  "search_depth": "basic",     // basic, fast, ultra-fast: 1 credit; advanced: 2
  "topic": "general",          // general, news or finance
  "include_answer": "",        // "", basic or advanced
  "country": "",               // e.g. "germany" - general topic only
  "language": "",              // e.g. "de"
  "timeout_seconds": 30
}
```

## What is not here

No extraction, crawling or research endpoints - `web_fetch` already reads pages - and no
images.
