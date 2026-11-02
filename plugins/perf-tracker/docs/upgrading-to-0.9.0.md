# Upgrading to 0.9.0

0.9.0 renames the plugin from `claude-performance-tracker` to `perf-tracker`.
Claude Code treats a renamed plugin as a new one, so `claude plugin update`
cannot reach it: you install `perf-tracker` and then uninstall the old plugin.
Do the steps in this order. Uninstalling the old plugin deletes its data
directory, and the new plugin copies its history from that directory.

**1. Back up the database.**

```bash
cp ~/.claude/plugins/data/claude-performance-tracker-*/usage.db ~/usage.db.bak
```

**2. Install the new plugin.**

```bash
claude plugin marketplace update agent-toolbox
claude plugin install perf-tracker@agent-toolbox
```

**3. Start a new Claude Code session.** Its SessionStart hook creates the new
data directory and copies in the history from the most populated
`claude-performance-tracker*` directory. Check that the history arrived:

```bash
sqlite3 ~/.claude/plugins/data/perf-tracker-agent-toolbox/usage.db \
  "select count(*) from turns"
```

The count should match the old database:

```bash
sqlite3 ~/.claude/plugins/data/claude-performance-tracker-agent-toolbox/usage.db \
  "select count(*) from turns"
```

**4. Uninstall the old plugin.**

> **Warning:** this deletes
> `~/.claude/plugins/data/claude-performance-tracker-*`. Run it only after step 3
> shows the history in the new directory.

```bash
claude plugin uninstall claude-performance-tracker@agent-toolbox
```

**5. Use the new skill names.** Skills are now `/perf-tracker:track`,
`/perf-tracker:usage-report` and so on. The `cpt` command is unchanged.

If step 3 shows no turns, the copy failed and the new plugin started with an
empty database. Before step 4, copy the backup over it with the session closed:

```bash
cp ~/usage.db.bak ~/.claude/plugins/data/perf-tracker-agent-toolbox/usage.db
```
