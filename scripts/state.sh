#!/usr/bin/env bash
# Sync youtube_automation.db with the `state` branch.
#   scripts/state.sh pull   -> fetch the DB (or start empty on first run)
#   scripts/state.sh push   -> integrity-check, then force-push a single-commit `state` branch
# The branch keeps only the latest snapshot (no history growth); point-in-time history lives in
# the weekly/daily backup releases (backup.yml). Writers are serialised by the workflows'
# `concurrency: state-db` group, so a force-push never overwrites a parallel writer.
set -euo pipefail

DB="${YT_DB_PATH:-youtube_automation.db}"
PY="${PYTHON:-python}"

case "${1:-}" in
  pull)
    if git ls-remote --exit-code --heads origin state >/dev/null 2>&1; then
      git fetch --depth=1 origin state
      git show FETCH_HEAD:youtube_automation.db > "$DB"
      echo "state: pulled $(du -h "$DB" | cut -f1) database"
    else
      echo "state: no state branch yet; starting with an empty database"
    fi
    ;;
  push)
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
    git push -qf origin "$commit:refs/heads/state"
    echo "state: pushed $commit"
    ;;
  *)
    echo "usage: $0 pull|push" >&2; exit 2 ;;
esac
