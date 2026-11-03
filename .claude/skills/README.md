# Local BokaClaw skills

Skills for this machine's Docker/Colima deployment. Kept here rather than in
the public `skills/` directory because they describe local infrastructure, not
the published SDK. Claude Code loads skills in this directory automatically.

| Skill | Use when |
|---|---|
| [`bokaclaw-docker-stack`](bokaclaw-docker-stack/SKILL.md) | Building or rebuilding the split images, or starting, stopping, migrating and health-checking the local Postgres + server + dashboard containers |

Scripts (also documented in the skill):

```bash
.claude/skills/bokaclaw-docker-stack/scripts/build-images.sh [all|server|dashboard]
.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh
.claude/skills/bokaclaw-docker-stack/scripts/stop-stack.sh [--remove]
```

All scripts resolve the repository root from their own location, so they can
be invoked from any working directory.
