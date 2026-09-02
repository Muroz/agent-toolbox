# prefs

Personal rules that apply to every Claude Code session, in every project. They
live here so they are version-controlled and reviewable instead of stranded in
an untracked dotfile.

| File | What it covers |
|------|----------------|
| [writing-style.md](writing-style.md) | Orwell's six rules, plus response shape, honest reporting, code references and decisions |
| [no-ai-attribution.md](no-ai-attribution.md) | Keep Claude and Anthropic out of commits, PRs, comments and changelogs |

## Install

```bash
./scripts/prefs.sh install
```

That symlinks every `prefs/*.md` into `$CLAUDE_CONFIG_DIR/rules/` (`~/.claude/rules/`
by default), which Claude Code loads unconditionally at the start of every
session. Edits here are live on the next session launch — nothing to reinstall.

The installer derives the repo root from its own location, so the clone can sit
anywhere. On a new machine, clone and run it. **If you move the repo the links
dangle**; `./scripts/prefs.sh status` reports it and `install` repairs it. Use
`install --copy` instead when the clone is temporary and you would rather the
rules outlive it.

`uninstall` removes only what this repo put there. Anything you edited in place
is kept, not clobbered — `install` refuses to overwrite it until you copy the
change back here or pass `--force`.

## Why rules and not a plugin

The rest of this repo is a plugin marketplace, but a plugin cannot ship
always-on instructions. Plugins provide skills (which load on demand), agents,
hooks and output styles — none of which puts a rule in context every session.
`~/.claude/rules/` does.
