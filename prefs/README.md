# prefs

Personal rules that apply to every Claude Code session, in every project. They
live here so they stay version-controlled and reviewable instead of stranded in
an untracked dotfile.

| File | What it covers |
|------|----------------|
| [writing-style.md](writing-style.md) | Orwell's six rules, plus response shape, honest reporting, code references, decisions and documents |
| [no-ai-attribution.md](no-ai-attribution.md) | Keep Claude and Anthropic out of commits, PRs, comments and changelogs |

## Install

```bash
./scripts/prefs.sh install
```

The installer symlinks every `prefs/*.md` into `$CLAUDE_CONFIG_DIR/rules/`,
which defaults to `~/.claude/rules/`. Claude Code loads that directory at the
start of every session. Edits here take effect on the next session launch, with
nothing to reinstall.

The installer derives the repo root from its own location, so the clone can sit
anywhere. On a new machine, clone the repo and run it.

Moving the repo breaks the links. Run `./scripts/prefs.sh status` to see which
ones dangle, then `install` to repair them. If the clone is temporary and you
want the rules to outlive it, run `install --copy` instead.

`uninstall` removes only what this repo put there. It keeps anything you edited
in place rather than overwriting it, and `install` refuses to overwrite such a
file until you copy the change back here or pass `--force`.

## Why rules and not a plugin

The rest of this repo is a plugin marketplace, but a plugin cannot ship
always-on instructions. Plugins provide skills, which load on demand, along with
agents, hooks and output styles. None of those puts a rule in context every
session. `~/.claude/rules/` does.
