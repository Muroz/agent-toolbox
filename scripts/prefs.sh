#!/usr/bin/env bash
#
# Install this repo's personal rules (prefs/*.md) into the Claude config
# directory, so they load into every session in every project.
#
# The repo root is derived from this script's own location, so the clone can
# live anywhere. Move the repo, re-run `install`, and the links are repaired.
#
# NOTE: this is not a hook. Unlike the ingest entrypoints described in the root
# CLAUDE.md, it must report failure honestly and exit nonzero when it fails.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SRC_DIR="$REPO/prefs"
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
RULES="$CONFIG/rules"
MANIFEST="$RULES/.agent-toolbox-prefs"

MODE=link
DRY_RUN=0
FORCE=0
CMD=""

usage() {
  cat <<'USAGE'
Usage: prefs.sh <command> [options]

Commands:
  install      Link every prefs/*.md into <config>/rules/   (default)
  status       Report the state of each rule; exit 1 if any is broken
  uninstall    Remove only the rules this repo installed

Options:
  --copy       install: copy instead of symlink (for a temporary clone)
  --dry-run    Print the planned actions without touching the filesystem
  --force      install: overwrite a rule this repo does not own, or one that
               was edited in place
  -h, --help   This message

The config directory is $CLAUDE_CONFIG_DIR, or ~/.claude when that is unset.
USAGE
}

# Basenames of the rules to install: every prefs/*.md except the directory README.
rule_names() {
  local path name
  for path in "$SRC_DIR"/*.md; do
    [ -e "$path" ] || continue
    name="$(basename "$path")"
    [ "$name" = "README.md" ] && continue
    printf '%s\n' "$name"
  done
}

in_manifest() {
  [ -f "$MANIFEST" ] && grep -qxF "$1" "$MANIFEST"
}

# One of: linked, dangling, foreign-link, copied, modified, foreign, missing
#
# Ownership is deliberately narrow. A regular file counts as ours only when the
# manifest lists it AND its bytes still match the repo source; the moment you
# edit it by hand it becomes `modified` and install refuses to touch it. Being
# named in the manifest is not enough on its own — that would let a re-run
# silently discard local edits.
state_of() {
  local name="$1" dest="$RULES/$1" target
  if [ -L "$dest" ]; then
    target="$(readlink "$dest")"
    if [ "$target" = "$SRC_DIR/$name" ]; then
      [ -e "$dest" ] && echo linked || echo dangling
    elif [ "$(basename "$(dirname "$target")")" = "prefs" ]; then
      # A link from a different clone of this repo. Ours to repoint.
      [ -e "$dest" ] && echo foreign-link || echo dangling
    else
      echo foreign
    fi
  elif [ -e "$dest" ]; then
    if in_manifest "$name"; then
      cmp -s "$dest" "$SRC_DIR/$name" && echo copied || echo modified
    else
      echo foreign
    fi
  else
    echo missing
  fi
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  would: %s\n' "$*"
  else
    "$@"
  fi
}

cmd_install() {
  local name state dest rc=0 planned=""

  for name in $(rule_names); do
    state="$(state_of "$name")"
    dest="$RULES/$name"

    if [ "$state" = foreign ] && [ "$FORCE" -eq 0 ]; then
      printf '  %-24s CONFLICT  %s exists and did not come from this repo\n' "$name" "$dest" >&2
      rc=1
      continue
    fi

    if [ "$state" = modified ] && [ "$FORCE" -eq 0 ]; then
      printf '  %-24s CONFLICT  %s was edited in place; copy the change back to %s first\n' \
        "$name" "$dest" "$SRC_DIR/$name" >&2
      rc=1
      continue
    fi

    if [ "$state" = linked ] && [ "$MODE" = link ]; then
      printf '  %-24s ok\n' "$name"
      planned="$planned$name"$'\n'
      continue
    fi

    printf '  %-24s %s -> %s\n' "$name" "$MODE" "$dest"
    run mkdir -p "$RULES"
    run rm -f "$dest"
    if [ "$MODE" = copy ]; then
      run cp "$SRC_DIR/$name" "$dest"
    else
      run ln -s "$SRC_DIR/$name" "$dest"
    fi
    planned="$planned$name"$'\n'
  done

  if [ "$rc" -ne 0 ]; then
    printf 'Refused to overwrite. Re-run with --force to replace it.\n' >&2
    return "$rc"
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  would: write manifest %s\n' "$MANIFEST"
  else
    printf '%s' "$planned" > "$MANIFEST"
    printf 'Installed into %s. Rules load on the next session launch.\n' "$RULES"
  fi
}

cmd_status() {
  local name state rc=0
  printf 'repo   %s\n' "$REPO"
  printf 'rules  %s\n\n' "$RULES"
  for name in $(rule_names); do
    state="$(state_of "$name")"
    printf '  %-24s %s\n' "$name" "$state"
    case "$state" in
      linked|copied) ;;
      *) rc=1 ;;
    esac
  done
  [ "$rc" -ne 0 ] && printf '\nRun `%s install` to repair.\n' "$0" >&2
  return "$rc"
}

cmd_uninstall() {
  local name state dest
  for name in $(rule_names); do
    state="$(state_of "$name")"
    dest="$RULES/$name"
    case "$state" in
      linked|dangling|foreign-link|copied)
        printf '  %-24s remove %s\n' "$name" "$dest"
        run rm -f "$dest"
        ;;
      modified)
        printf '  %-24s kept (edited in place, would lose the change)\n' "$name"
        ;;
      foreign)
        printf '  %-24s kept (not ours)\n' "$name"
        ;;
      missing)
        printf '  %-24s absent\n' "$name"
        ;;
    esac
  done
  run rm -f "$MANIFEST"
}

while [ $# -gt 0 ]; do
  case "$1" in
    install|status|uninstall) CMD="$1" ;;
    --copy)    MODE=copy ;;
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'prefs.sh: unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -d "$SRC_DIR" ] || { printf 'prefs.sh: no prefs directory at %s\n' "$SRC_DIR" >&2; exit 1; }

case "${CMD:-install}" in
  install)   cmd_install ;;
  status)    cmd_status ;;
  uninstall) cmd_uninstall ;;
esac
