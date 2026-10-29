# mr-review-agent

Polls GitLab for MRs where your review was requested, runs your
`/staff-engineer-review` skill on each one, and posts the write-up as a **draft
note** — visible only to you until you publish it.

No install step. `mra.py`, `prompt.md` and `config.json` live in one folder; run
the script from anywhere.

## Setup

1. `cp config.example.json config.json`, then edit `gitlab_url` and `repos` (or
   `search_roots`). `config.json` is gitignored — credentials, never commit it.
2. Save a PAT with `read_api` + `read_repository` as `token` in this folder:

```bash
echo 'glpat-xxxx' > token && chmod 600 token
```

## Run

```bash
python3 mra.py --once --dry-run    # look, don't post
python3 mra.py                     # poll every 5 min
```

Leave the second one in a terminal tab, or `nohup` it. Ctrl-C stops it.

## What happens per MR

1. `GET /todos?action=review_requested&type=MergeRequest&state=pending`
2. Fetch `refs/merge-requests/<iid>/head` (works for fork MRs) + the target branch.
3. Add a worktree detached at the **target branch tip** — your own clone is never
   checked out, destructively fetched, or modified.
4. `claude -p "/staff-engineer-review …"` with read-only tools, in that worktree.
5. Post a draft note, mark the todo done, record the SHA, remove the worktree.

A new push to the same MR is re-reviewed; the same SHA never runs twice.
`state.json`, `logs/` and `worktrees/` appear next to the script on first run.

A macOS banner fires when a draft review is posted (`"notify": false` in
`config.json` to silence it).

## Files

- `mra.py` — everything
- `prompt.md` — the prompt, with `{{iid}}`, `{{base_sha}}`, `{{mr_ref}}` placeholders
- `config.example.json` — copy to `config.json` and edit
- `test_e2e.py` — stub GitLab API + fake `claude` + real git repos; run after edits
