# agent-toolbox

A personal Agent toolbox marketplace (Primarily for [Claude Code](https://code.claude.com)). A monorepo for hosting plugins, skills, hooks, subagents and anything else I'm currently testing as I go deeper into AI intensive workflows under one installable source.

## Layout

```
agent-toolbox/
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
claude plugin marketplace add ~/Coding/agent-toolbox

# Then install any plugin individually
claude plugin install claude-performance-tracker@agent-toolbox
```

Update later with `claude plugin marketplace update agent-toolbox`.

## Plugins

| Plugin | Description |
|--------|-------------|
| [claude-performance-tracker](plugins/claude-performance-tracker) | Measure and qualify how you use agents — token/time/prompt cost per successful outcome, approach comparison, prompt quality, and model-degradation trends. |

## Development

### Adding a new plugin

1. Create a subdirectory under `plugins/<name>/` with at least
   `.claude-plugin/plugin.json` (the `name` field is required).
2. Add an entry to `.claude-plugin/marketplace.json` with an **explicit relative
   source**: `"source": "./plugins/<name>"`.
   > Use the explicit `./plugins/...` path form. The `metadata.pluginRoot` +
   > bare-name shorthand is rejected by some Claude Code versions
   > ("source type your Claude Code version does not support").
3. A plugin can be atomic — just a skill, just a `hooks/hooks.json`, or just an
   agent. Only `plugin.json` (`name`) is strictly required.

### Iterate without installing (fastest loop)

Load the plugin straight from the working tree for a single session — picks up
your latest edits each launch, no reinstall, no cache:

```bash
claude --plugin-dir ~/Coding/agent-toolbox/plugins/claude-performance-tracker
```

Repeatable for multiple plugins (`--plugin-dir A --plugin-dir B`).

### Refresh the installed copy

The marketplace caches a **snapshot** of the plugin at its `version`. `claude
plugin update` is a no-op while the version is unchanged, so for same-version dev
edits, reinstall:

```bash
claude plugin uninstall claude-performance-tracker@agent-toolbox
claude plugin marketplace update agent-toolbox
claude plugin install claude-performance-tracker@agent-toolbox
```

Alternatively, bump `version` in both the plugin's `plugin.json` and its
`marketplace.json` entry, then:

```bash
claude plugin marketplace update agent-toolbox
claude plugin update claude-performance-tracker@agent-toolbox   # restart to apply
```

Verify what's installed: `claude plugin details claude-performance-tracker@agent-toolbox`
(shows the component inventory: skills, agents, hooks).

### Run the tests

Dependency-free (stdlib `unittest`), runnable with just `python3`:

```bash
cd plugins/claude-performance-tracker
python3 -m unittest discover -s tests
```
