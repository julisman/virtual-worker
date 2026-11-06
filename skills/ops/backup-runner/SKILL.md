---
name: Backup Runner
description: Back up the working folder with shell tar — dated .tar.gz in a backups/ subfolder, junk excluded, archive verified by listing and file-count comparison, keeping the last N (default 5) with rotation only after the new archive verifies.
when_to_use: When the user asks to back up, snapshot, or archive the working folder or a project in it, or when a scheduled backup run fires.
version: 1.0.0
---

# Backup Runner

Create a verified, dated archive of the working folder in `backups/`, then rotate old archives — never deleting anything outside `backups/`.

## Inputs

- **Source** — default: the working folder root (a subfolder if the user names one).
- **Keep count N** — how many archives to retain. Default: 5.
- **Extra excludes** — optional user-named paths to skip, added to the standard junk list.

## Steps

1. Confirm the `shell` tool is available. If it is off, stop and tell the user to enable the shell tool for this agent — `tar` archiving needs it; there is no file-tool fallback for a real backup.
2. Prepare: `shell` `mkdir -p backups`, then build the archive name `backups/backup-<YYYY-MM-DD-HHMM>.tar.gz` (real current date-time).
3. Create the archive from the source dir via `shell`:
   `tar -czf backups/backup-<stamp>.tar.gz --exclude='./backups' --exclude='./node_modules' --exclude='./.git' --exclude='./.venv' --exclude='./venv' --exclude='*.log' --exclude='./dist' --exclude='./__pycache__' <extra excludes> .`
   Excluding `./backups` itself is mandatory — never archive old backups into new ones.
4. Verify the new archive, both checks required before any rotation:
   a. `tar -tzf backups/backup-<stamp>.tar.gz > /dev/null` exits 0 (readable, not corrupt), and `tar -tzf ... | grep -cv '/$'` gives the archived file count.
   b. Expected count: `find . -type f -not -path './backups/*' -not -path './node_modules/*' -not -path './.git/*' -not -path './.venv/*' -not -path './venv/*' -not -name '*.log' -not -path './dist/*' -not -path './__pycache__/*' | wc -l` (mirror any extra excludes). Counts must match; a small delta is only acceptable if you can name the exact files that changed mid-run.
5. If verification fails: delete only the bad new archive in `backups/`, report the failure, and **do not rotate** — the previous archives are the only good copies.
6. Rotate only after verification passes: `ls -1t backups/backup-*.tar.gz` and identify archives beyond the newest N. **The first time a rotation would ever delete anything, list the candidate files and ask the user before deleting**; record their standing OK with `memory_write` (`backup rotation approved, keep last <N> | <YYYY-MM-DD>`), and on later runs `memory_search` for that approval before deleting silently. Delete only matching `backups/backup-*.tar.gz` files.
7. Report: archive path, size (`du -h`), archived file count, verification result, and what was rotated out.

## Output format

```
Backup complete — <source>
Archive:  backups/backup-<stamp>.tar.gz  (<size>)
Files:    <archived count> archived · expected <count> · verified OK
Rotation: kept last <N>; removed <list or "nothing">
```

On failure: `Backup FAILED at <step> — <error>. Previous archives untouched.`

## Rules

- **Never delete anything outside `backups/`**, and inside it only files matching `backup-*.tar.gz`. No `rm -rf`, no wildcards outside that pattern.
- Rotation strictly after the new archive verifies — a failed backup must never cost an old one.
- First-ever rotation delete requires the user's explicit OK (step 6); scheduled runs before that OK exists keep everything and flag that `backups/` is growing.
- Junk exclude list is the default (node_modules, .git, venvs, `*.log`, dist, __pycache__, backups itself); the user can add excludes but sources are never modified — this skill only creates archives.
- If the source is unexpectedly huge (archive step very slow or the count is >100k files), stop and confirm scope with the user rather than filling the disk.
- Local-first: archives stay in the working folder's `backups/`; no uploads anywhere.
