"""The github skill, carried by a plugin - see PLUGIN.md.

The procedure is `skill/SKILL.md`, an ordinary skill file a person can copy into
`~/.ultron/skills/` and edit. The fields below restate its frontmatter, because the
SDK hands a plugin a `Skill` to fill in rather than a file to read; the tests hold
the two together.
"""

from __future__ import annotations

from pathlib import Path

from ultron.sdk.plugin_entry import Plugin, PluginContext
from ultron.sdk.skill_plugin import Requires, Skill

SKILL_DIR = Path(__file__).resolve().parent / "skill"


def skill() -> Skill:
    return Skill(
        name="github",
        description=(
            "GitHub through the gh CLI - issues, pull requests, CI runs and failed logs, "
            "reviews, releases, and gh api queries."
        ),
        directory=SKILL_DIR,
        requires=Requires(bins=("gh",), tools=("exec",)),
        emoji="🐙",
        homepage="https://cli.github.com",
    )


class GithubPlugin(Plugin):
    name = "github"
    description = "The github skill."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_skill(skill())
