# claude-toolbox

A personal [Claude Code](https://code.claude.com) marketplace — a monorepo that hosts
multiple plugins (and smaller atomic pieces: lone skills, hooks packs, single subagents)
under one installable source.

## Layout

```
claude-toolbox/
├── .claude-plugin/marketplace.json   # lists every installable piece
└── plugins/
    └── claude-performance-tracker/   # plugin #1
```

New pieces are added as subdirectories under `plugins/` and registered as entries in
`marketplace.json`. Because `metadata.pluginRoot` is `./plugins`, each entry's `source`
is just the subdirectory name.

## Install

```bash
# Add this marketplace once (from a local clone, GitHub shorthand, or git URL)
claude plugin marketplace add ~/Coding/claude-toolbox

# Then install any plugin individually
claude plugin install claude-performance-tracker@claude-toolbox
```

Update later with `claude plugin marketplace update claude-toolbox`.

## Plugins

| Plugin | Description |
|--------|-------------|
| [claude-performance-tracker](plugins/claude-performance-tracker) | Measure and qualify how you use agents — token/time/prompt cost per successful outcome, approach comparison, prompt quality, and model-degradation trends. |
