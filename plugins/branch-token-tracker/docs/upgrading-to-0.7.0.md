# Upgrading to 0.7.0

Message for testers. Paste it as is.

---

**branch-token-tracker 0.7.0 is out: please upgrade before your next export**

Reports now show why a ticket cost what it did (model calls per turn, context
size and idle gaps), the title of each unassigned session, each ticket's pull
requests and a corrected dollar estimate. The old Sonnet 5 price was too high,
and the newest models weren't priced at all.

**1. Back up your data.** Uninstalling deletes it. Updating doesn't, but a copy
costs nothing:

```bash
cp ~/.claude/plugins/data/branch-token-tracker-*/tokens.db ~/tokens.db.bak
```

**2. Update the plugin**, then restart Claude Code:

```bash
claude plugin marketplace update agent-toolbox
claude plugin update branch-token-tracker@agent-toolbox
```

**3. Add two settings** to `~/.claude/branch-tokens.json`. Create the file if
you don't have one, and keep any `patterns` you already set:

```json
{
  "person": "your-name-or-alias",
  "billing": "subscription"
}
```

`person` labels your rows so exports can be told apart. `billing` is
`subscription` if you use a Claude plan, or `api` if you pay per token. Without
it, the plugin guesses, and it reads anyone with `ANTHROPIC_API_KEY` set as
`api`.

**4. Fill in your history:**

```bash
btt backfill
```

This only reaches sessions from about the last 30 days, because Claude Code
deletes older transcripts. Older work shows "—" in the new columns.

**5. Before each export, fetch PR data.** This needs the GitHub CLI, logged in
with `gh auth login`:

```bash
btt enrich
```

**6. Export as before:**

```bash
btt report --format csv > tokens.csv
btt report --by day --split ticket --format csv > tokens-daily.csv
```

Exports leave out session titles and prompts by default. Only add
`--include-text` if asked for it.

> **Warning:** read an `--include-text` export before you send it. Prompts can
> contain secrets or customer data.

If anything looks off, run any `btt` command with `BTT_DEBUG=1` in front and
send the output.

---

The commands assume the tester registered the marketplace as `agent-toolbox`.
If they used a different name, change it in step 2.
