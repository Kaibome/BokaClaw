# Local BokaClaw skills

Skills for this machine's Docker/Colima deployment. Kept here rather than in
the public `skills/` directory because they describe local infrastructure, not
the published SDK. Claude Code loads skills in this directory automatically.

| Skill | Use when |
|---|---|
| [`build-bokaclaw-images`](build-bokaclaw-images/SKILL.md) | Building or rebuilding `bokaclaw-server:test` / `bokaclaw-dashboard:test`, or a Docker build is stuck on package downloads |
| [`run-bokaclaw-stack`](run-bokaclaw-stack/SKILL.md) | Starting, stopping, migrating or health-checking the three local containers |

Each skill has a `scripts/` directory that implements the same steps:

```bash
.claude/skills/build-bokaclaw-images/scripts/build-images.sh [all|server|dashboard]
.claude/skills/run-bokaclaw-stack/scripts/start-stack.sh
.claude/skills/run-bokaclaw-stack/scripts/stop-stack.sh [--remove]
```

Both scripts resolve the repository root from their own location, so they can
be invoked from any working directory.
