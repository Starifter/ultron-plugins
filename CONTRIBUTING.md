# Contributing a plugin

A plugin is one directory at the top of this repository. Its name is the directory's name
and the `name:` in its `PLUGIN.md`; they have to agree, because the manifest's name is what
`plugins_enabled` carries and the directory's name is what `/plugins install` looks up.

```
dice/
  PLUGIN.md     the manifest - YAML frontmatter, then prose a person reads
  plugin.py     exactly one Plugin subclass, or a module-level `plugin` instance
  logo.svg      optional - named by `logo:` in the manifest
```

## The manifest

```yaml
---
name: dice
description: One line, shown in the Discover tab.
version: "1.0.0"
requires_ultron_sdk: ">=1,<2"
categories: [fun]
contracts:
  tools: [roll_dice]
config_schema:
  max_dice:
    type: int
    default: 100
    description: What a setting does, shown by /plugins dice.
---
```

- `version` is yours and is required here so an update is visible as one.
- `requires_ultron_sdk` is a range over `ultron.sdk.SDK_VERSION`, which is `MAJOR.MINOR`.
  Ultron refuses an incompatible plugin outright and says so; declare what you use.
- `contracts` says what `register` will install. Ultron checks the two agree after
  `register` runs, and a plugin that installs nothing is an error.
- `config_schema` is every setting the plugin reads with `ctx.setting(...)`. Declared
  settings are shown, typed and defaulted by `/plugins <name>`; undeclared ones warn.
- `autoload` is refused. Ultron honours it only for the plugins it ships itself.
- `python_dependencies` is surfaced and never installed - say what you import so a
  missing module is a diagnosis rather than a traceback.

## The code

Import from `ultron.sdk.*` only. The subpaths are the public surface and are versioned;
`ultron.plugins`, `ultron.tools` and the rest are not, and a plugin that reaches into them
breaks on a release that owes it nothing.

Two obligations come with a tool, and Ultron cannot check either for you: a tool that
commits a side effect calls `assert_active()` first (`ultron.sdk.runtime`), and a tool that
spawns a process uses `scrubbed_environment()`. A tool whose result carries bytes from off
this machine sets `untrusted = True`. Tools return a `ToolResult` for failures too - an
error is something the model can see and recover from, not an exception.

A credential is never a setting: `config.json` refuses a credential-shaped key. Read a key
from the environment (`~/.ultron/.env` is loaded into it) and name the variable in your
manifest's prose, as `brave` does. A plugin that calls a **model vendor** Ultron already
has a key for - OpenAI, Google - asks for it with `ctx.credential("openai")` instead (SDK
1.38), and lists the vendor under `vendor_credentials` in its manifest; any vendor it did
not list is refused. Ask when the request is made, not in `register`, as `imagegen` does.

## Extending another plugin

A plugin can be extended by plugins it has never heard of (SDK 1.39). The owner reads a
point named for itself - `ctx.extensions_in("imagegen.backend")` - and anyone writes into
it with `ctx.register_extension("imagegen.backend", "acme", build)`. The writer needs no
`requires_plugins` and no import: with the owner absent, the entry sits unread. That is how
`xai`, `openrouter`, `together` and `fireworks` give `imagegen` and `videogen` their
vendors and `openrouter` gives `musicgen` one, and how a new image, video or music vendor
joins without a change to any of them.

- **Owning a point:** publish the interface in your `PLUGIN.md`, as `imagegen`,
  `videogen` and `musicgen` do under *Adding a vendor*, and treat what you read as code you did not write:
  read it when you need it (never in `register`), duck-type it, bound every call, and turn
  whatever it raises into a result.
- **Writing into one:** guard with `if hasattr(ctx, "register_extension"):` so the plugin
  still loads on an older Ultron, and leave the entry out of `contracts`, which an older
  Ultron warns about. Register a builder rather than a built object when the owner's
  interface asks for one, so a key is read only when the owner reaches you.

## Before opening a pull request

From a checkout of Ultron beside this one:

```
uv run --project ../Ultron python scripts/validate.py
uv run --project ../Ultron pytest <name>/tests
```

The script is the whole of what this marketplace promises about an entry. Tests are yours
to add and welcome: put them in `<name>/tests/`, driving the plugin with a fake client the
way `openrouter/tests/` does (asyncio in auto mode, from the `pyproject.toml` at the root).
`ruff check .` and `ruff format .` read Ultron's own settings from the same file, so a
plugin reads like the code it plugs into.

CI runs the script and the tests too, but it installs Ultron from its repository, which is
private for now, so the `validate` job fails before it reaches your plugin; `pip install
git+https://github.com/Starifter/ultron.git` works only for someone with access to it. Run
both locally, and say in the pull request that you did.

Then try it: `/plugins market refresh official` on an install whose
`plugin_marketplace_official` points at your checkout, `/plugins install <name>`, restart,
and `/plugins <name>` should show what it installed.
