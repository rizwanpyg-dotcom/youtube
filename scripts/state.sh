#!/usr/bin/env bash
# Sync youtube_automation.db with the `state` branch.
#   scripts/state.sh pull   -> fetch the DB (or start empty on first run)
#   scripts/state.sh push   -> integrity-check, then force-push a single-commit `state` branch
# The branch keeps only the latest snapshot (no history growth); point-in-time history lives in
# the weekly/daily backup releases (backup.yml). Writers are serialised by the workflows'
# `concurrency: state-db` group, so a force-push never overwrites a parallel writer.
set -euo pipefail

DB="${YT_DB_PATH:-youtube_automation.db}"
BASE="${DB}.state-base"   # which state commit this run started from
PY="${PYTHON:-python}"

case "${1:-}" in
  pull)
    # Only a definite "branch does not exist" (ls-remote exit 2) may start an empty database.
    # Any other failure (network, auth, GitHub outage) must fail the run: starting empty and then
    # pushing would overwrite the real state.
    for attempt in 1 2 3 4 5; do
      set +e; git ls-remote --exit-code --heads origin state >/dev/null 2>&1; rc=$?; set -e
      [ "$rc" = 0 ] || [ "$rc" = 2 ] && break
      echo "state: ls-remote failed (exit $rc), retry $attempt/5"; sleep $((attempt * 10))
    done
    if [ "$rc" = 0 ]; then
      for attempt in 1 2 3 4 5; do
        git fetch --depth=1 origin state && break
        [ "$attempt" = 5 ] && { echo "state: fetch failed; refusing to continue" >&2; exit 1; }
        sleep $((attempt * 10))
      done
      git show FETCH_HEAD:youtube_automation.db > "$DB"
      git rev-parse FETCH_HEAD > "$BASE"
      echo "state: pulled $(du -h "$DB" | cut -f1) database (base $(cut -c1-7 "$BASE"))"
    elif [ "$rc" = 2 ]; then
      echo "none" > "$BASE"
      echo "state: no state branch yet; starting with an empty database"
    else
      echo "state: cannot reach the state branch (exit $rc); refusing to start with an empty database" >&2
      exit 1
    fi
    ;;
  push)
    # Never overwrite state this run didn't start from (lost pull, or a writer we don't know about).
    [ -f "$BASE" ] || { echo "state: no pull recorded for this run; NOT pushing" >&2; exit 1; }
    set +e; remote="$(git ls-remote --heads origin state | cut -f1)"; rc=$?; set -e
    [ "$rc" = 0 ] || { echo "state: cannot check the remote state; NOT pushing" >&2; exit 1; }
    base="$(cat "$BASE")"
    if [ -n "$remote" ] && [ "$remote" != "$base" ]; then
      echo "state: remote state changed since this run started ($base -> $remote); NOT pushing" >&2
      exit 1
    fi
    "$PY" - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
res = c.execute("PRAGMA integrity_check").fetchone()[0]
c.execute("PRAGMA journal_mode=DELETE")
c.close()
if res != "ok":
    sys.exit(f"state: integrity check failed ({res}); NOT pushing")
print("state: integrity ok")
PY
    # Build a parentless commit with git plumbing: no worktree, no temporary branch, and the
    # working tree is never touched.
    db_blob="$(git hash-object -w "$DB")"
    readme_blob="$(printf '# Pipeline state\n\nMachine-written SQLite database. Do not edit by hand.\n' | git hash-object -w --stdin)"
    tree="$(printf '100644 blob %s\tREADME.md\n100644 blob %s\tyoutube_automation.db\n' "$readme_blob" "$db_blob" | git mktree)"
    commit="$(GIT_AUTHOR_NAME=ytengine-bot GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com' \
              GIT_COMMITTER_NAME=ytengine-bot GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com' \
              git commit-tree "$tree" -m "state: $(date -u +%Y-%m-%dT%H:%M:%SZ) run ${GITHUB_RUN_ID:-local}")"
    if [ -n "$remote" ]; then
      git push -q --force-with-lease="refs/heads/state:$remote" origin "$commit:refs/heads/state"
    else
      git push -q origin "$commit:refs/heads/state"
    fi
    echo "$commit" > "$BASE"
    echo "state: pushed $commit"
    ;;
  *)
    echo "usage: $0 pull|push" >&2; exit 2 ;;
esac
