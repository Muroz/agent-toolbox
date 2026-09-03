# No Claude / AI attribution in code-related output

Do NOT mention Claude, Anthropic, "AI assistant" or any equivalent involvement in
anything code-related, unless I explicitly ask for it in a specific case. This applies to:

- Git commit messages — no `Co-Authored-By: Claude ...` trailer, no AI mention in the subject or body.
- PR titles and descriptions — no `🤖 Generated with [Claude Code](...)` footer, no "authored/assisted by Claude" lines.
- Code comments, docstrings, changelogs, release notes, issue/ticket text and any other code or repo artifact.

Write everything as if I authored it directly. If a hook, template or default would inject
such attribution automatically, omit it — and if you can't, flag it to me rather than letting it through.
