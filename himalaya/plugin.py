"""The himalaya skill, carried by a plugin - see PLUGIN.md.

The procedure is `skill/SKILL.md` with its reference under `skill/references/`,
ordinary skill files a person can copy into `~/.ultron/skills/` and edit. The
fields below restate the frontmatter, because the SDK hands a plugin a `Skill` to
fill in rather than a file to read; the tests hold the two together.
"""

from __future__ import annotations

from pathlib import Path

from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.skill_plugin import Requires, Skill

SKILL_DIR = Path(__file__).resolve().parent / "skill"


def skill() -> Skill:
    return Skill(
        name="himalaya",
        description=(
            "Email over IMAP, SMTP, JMAP, Gmail or Graph with the himalaya CLI - list, "
            "search, read, compose, send, move, flag and delete."
        ),
        directory=SKILL_DIR,
        requires=Requires(bins=("himalaya",), tools=("exec",)),
        emoji="📧",
        homepage="https://github.com/pimalaya/himalaya",
    )


class HimalayaPlugin(Plugin):
    name = "himalaya"
    description = "The himalaya skill."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_skill(skill())
