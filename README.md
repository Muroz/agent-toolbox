# agent-toolbox

A personal plugin marketplace for [Claude Code](https://code.claude.com). It
holds plugins, skills, hooks and subagents in one repo, so any of them installs
from a single source.

## Layout

```
agent-toolbox/
├── .claude-plugin/marketplace.json   # lists every installable piece
├── prefs/                            # personal rules, loaded every session
├── scripts/prefs.sh                  # installs prefs/ into ~/.claude/rules/
└── plugins/
    ├── claude-performance-tracker/
    └── branch-token-tracker/
```

Add a new piece as a subdirectory under `plugins/` and register it in
`marketplace.json`.

## Plugins

| Plugin | What it does |
|--------|--------------|
| [claude-performance-tracker](plugins/claude-performance-tracker) | Measures how you use agents: token, time and prompt cost per successful outcome, approach comparison, prompt quality and model-degradation trends. |
| [branch-token-tracker](plugins/branch-token-tracker) | Attributes token spend to the task-tracker id in your git branch name, so you can answer what a ticket cost. |

## Install

```bash
# Add this marketplace once, from a local clone, GitHub shorthand or a git URL
claude plugin marketplace add /path/to/agent-toolbox

# Then install any plugin individually
claude plugin install claude-performance-tracker@agent-toolbox
```

To pick up later changes, run `claude plugin marketplace update agent-toolbox`.

> **Warning:** `claude plugin uninstall` deletes the plugin's data directory
> along with the plugin. Both trackers keep months of capture there. Back the
> database up first. See each plugin's README.

## Personal preferences

`prefs/` holds rules that apply to every session in every project. Install them
once per machine:

```bash
git clone https://github.com/Muroz/agent-toolbox.git
cd agent-toolbox && ./scripts/prefs.sh install
```

That links each `prefs/*.md` into `~/.claude/rules/`, which Claude Code loads at
the start of every session. See [prefs/README.md](prefs/README.md) for the
options and for why these are rules rather than a plugin.

## Development

### Add a new plugin

1. Create `plugins/<name>/` with at least `.claude-plugin/plugin.json`. The
   `name` field is the only required one.
2. Add an entry to `.claude-plugin/marketplace.json` with an explicit relative
   source: `"source": "./plugins/<name>"`.

   Use that path form. Some Claude Code versions reject the
   `metadata.pluginRoot` shorthand with "source type your Claude Code version
   does not support".
3. A plugin can be a single piece: one skill, one `hooks/hooks.json` or one
   agent. Only `plugin.json` is required.

### Iterate without installing

This is the fastest loop. Claude Code loads the plugin straight from the working
tree for one session and picks up your latest edits on each launch, with no
reinstall and no cache:

```bash
claude --plugin-dir /path/to/agent-toolbox/plugins/claude-performance-tracker
```

Repeat the flag for more than one plugin: `--plugin-dir A --plugin-dir B`.

### Refresh the installed copy

The marketplace caches a snapshot of the plugin at its `version`, so
`claude plugin update` does nothing while the version is unchanged. Bump
`version` in both the plugin's `plugin.json` and its `marketplace.json` entry,
then:

```bash
claude plugin marketplace update agent-toolbox
claude plugin update claude-performance-tracker@agent-toolbox   # restart to apply
```

An update preserves the plugin's data directory. Uninstalling and reinstalling
does not, so prefer the version bump.

To see what is installed, including the skills, agents and hooks it ships, run
`claude plugin details claude-performance-tracker@agent-toolbox`.

### Run the tests

Each plugin's suite uses the standard library `unittest` and has no
dependencies:

```bash
cd plugins/claude-performance-tracker
python3 -m unittest discover -s tests
```
