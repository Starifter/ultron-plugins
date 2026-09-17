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
manifest's prose, as `brave` does.

## Before opening a pull request

```
pip install git+https://github.com/Starifter/ultron.git
python scripts/validate.py
```

The same script runs in CI on every change and is the whole of what this marketplace
promises about an entry. Tests are yours to add and welcome: put them in
`<name>/tests/`, driving the plugin with a fake client the way `openrouter/tests/` does,
and CI runs them with `pytest` (asyncio in auto mode, from the `pyproject.toml` at the
root). Then try it: `/plugins market refresh official` on an install
whose `plugin_marketplace_official` points at your checkout, `/plugins install <name>`,
restart, and `/plugins <name>` should show what it installed.
