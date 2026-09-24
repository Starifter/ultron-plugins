# Ultron plugins

The official plugin marketplace for [Ultron](https://github.com/Starifter/ultron).
Every top-level directory here is one plugin: a `PLUGIN.md` manifest and a `plugin.py`
beside it, written against `ultron.sdk` and nothing else - exactly what the plugins
Ultron itself ships are made of.

Every Ultron install has this marketplace connected under the name `official`. Nothing is
fetched until you ask:

```
/plugins market refresh official      fetch or update the copy on this machine
/plugins                              Discover tab: what it offers
/plugins install dice                 copy one into ~/.ultron/plugins/ and enable it
```

Installing is two things said out loud - a copy into `~/.ultron/plugins/<name>/`, then a
line in `plugins_enabled` - and the plugin loads on the next session. A marketplace is
where a plugin comes from and never where one runs from: forgetting this marketplace
(`/plugins market remove official`) leaves every plugin installed from it in place.

## What is here

| plugin | what it adds |
|---|---|
| `openrouter` | A model provider: one `OPENROUTER_API_KEY`, every model OpenRouter routes to, with a live catalog of what each costs. |
| `llama-cpp` | A model provider for a `llama-server` on this machine: GGUF models, no key, no bill, with an embedder for memory search. |
| `dice` | A `roll_dice` tool - the example a new plugin is copied from. |

## What "official" means

Only that Ultron knows this repository's address. A plugin here goes through the same
refresh, the same copy and the same consent line as one from any other marketplace, and
nothing is allowed or refused because it came from here. What the marketplace promises is
the check in [`scripts/validate.py`](scripts/validate.py), run on every change: each entry
has a manifest that parses, a name that matches its directory, a version, an SDK range that
the current SDK satisfies, and no `autoload` - which Ultron refuses from anything it did
not ship, so a plugin here that said it would be a plugin lying about itself.

## Pointing an install elsewhere

`plugin_marketplace_official` in `config.json` is the address. A path to a checkout of this
repository reads it in place, which is how it is developed; `""` disconnects it.

```json
{ "plugin_marketplace_official": "~/dev/ultron-plugins" }
```

## Adding a plugin

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: copy `dice/`, rename
everything, make `scripts/validate.py` pass, open a pull request.
