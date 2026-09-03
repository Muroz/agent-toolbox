# agent-toolbox

Personal Claude Code plugins. Read this before touching anything under `hooks/`,
`bin/` or `scripts/ingest.py`.

## The hook contract: a hook may never exit nonzero

A hook's exit code is a **control channel, not a diagnostic**. On
`UserPromptSubmit` an exit of 2 does not report a problem — it *blocks the
user's prompt* and shows them the hook's stderr. A tracking plugin failing is
never worth refusing to run the user's session, so the ingest entrypoints have
exactly one guarantee that outranks doing their job: **exit 0, always.**

This is not theoretical. It happened:

> `UserPromptSubmit operation blocked by hook: ingest.py: error: argument
> --event: invalid choice: 'UserPromptSubmit'`

Every prompt in that session was refused until the session was abandoned.

### Why it happened — the asymmetry that makes this easy to trip

Claude Code **snapshots `hooks.json` when a session starts**, but the plugin's
**scripts are read live off disk** on every invocation. In dev mode
(`--plugin-dir`) or a `"source": "directory"` marketplace, "off disk" means the
working tree — so an edit is instantly live for every *already-running* session,
while their hook config stays frozen at whatever it was when they started.

So the moment `UserPromptSubmit` was removed from `hooks.json` *and* its handler
deleted from `ingest.py`, every open session was firing an event the new script
had been taught to reject. `argparse(choices=...)` rejected it with exit 2, and
exit 2 is the block signal.

Two supposed safety nets both missed:

- `except Exception` sat *below* `parse_args()`, and argparse raises
  `SystemExit`, which is not an `Exception`. Coverage started after the line
  that failed.
- The `cpt`/`btt` launcher used `exec`, so Python's exit code became the hook's
  exit code verbatim.

### The rules

1. **Never delete an event from `ingest.py`. Retire it.** Removing a hook from
   `hooks.json` must leave the name behind in `RETIRED` as a documented no-op,
   forever. Old sessions outlive the edit; a tombstone costs nothing.
2. **Unknown input is a no-op, never an error.** No `choices=`, no `required=`,
   `parse_known_args` — an event or flag we do not recognise is a stale config,
   which is normal, not a fault.
3. **The parser goes inside the `try`, and the catch is `BaseException`.**
   Coverage must start before the first line that can fail.
4. **Hooks only ever call `cpt ingest` / `btt ingest`,** the one subcommand
   hardened to exit 0. The launcher installs `trap 'exit 0' EXIT` before
   anything that can fail, and does not `exec` (an exec'd process carries no
   trap of ours).
5. **Stay silent by default.** stderr from a blocking hook event is user-facing.
   Diagnostics go behind `$CPT_DEBUG` / `$BTT_DEBUG`, which also exist so a
   broken plugin is distinguishable from a working one.

`tests/test_hook_safety.py` in each plugin enforces all of this, including that
every event ever shipped is still accounted for. Do not weaken it.

## Two plugin-loading modes, two different staleness traps

- **Dev mode** (`claude --plugin-dir ...`) runs scripts live from the working
  tree. Great for iteration; this is the mode that bricked the session above.
- **Installed** copies the plugin into a version-pinned snapshot at
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. It is a **copy**,
  not a link — editing this repo does *not* change an installed plugin, even for
  a `"source": "directory"` marketplace. Reaching an installed plugin takes a
  version bump in `.claude-plugin/marketplace.json` plus
  `claude plugin update <plugin>@agent-toolbox`.

## Tests

Stdlib `unittest`, no dependencies, per plugin:

    cd plugins/<plugin> && python3 -m unittest discover -s tests
