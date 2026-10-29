#!/usr/bin/env python3
"""Watch GitLab for MRs where my review was requested, review each one with
`claude -p /staff-engineer-review` in a throwaway worktree, and post the write-up
as a draft note.

    python3 mra.py                    # poll forever
    python3 mra.py --once             # one pass
    python3 mra.py --once --dry-run   # review, post nothing
    python3 mra.py --mr 1234 --dry-run

Needs a PAT with read_api + read_repository. stdlib only.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

DEFAULTS = {
    "claude_bin": "claude",
    "max_turns": 30,
    "max_budget_usd": 1.0,
    "allowed_tools": "Read,Grep,Glob,Bash(git *)",
    "poll_seconds": 300,
    "skip_drafts": True,
    "notify": True,
    "repos": {},
    "search_roots": [],
}


class Fatal(Exception):
    """Bad config or auth: stop the run."""


class Skip(Exception):
    """Nothing to do for this MR: try the next one."""


# --------------------------------------------------------------------------- #
# config, state, log
# --------------------------------------------------------------------------- #

def load_config(path):
    if not os.path.exists(path):
        raise Fatal("no config at %s" % path)
    cfg = dict(DEFAULTS)
    cfg.update(json.load(open(path)))

    base = os.path.expanduser(cfg.get("dir") or os.path.dirname(path))
    cfg["dir"] = base
    for key, default in (("workspace", "worktrees"), ("state_file", "state.json"),
                         ("log_file", "logs/mr-review.log"), ("prompt_file", "prompt.md")):
        cfg[key] = os.path.expanduser(cfg.get(key) or os.path.join(base, default))

    token = cfg.get("token") or os.environ.get("GITLAB_TOKEN", "")
    token_file = cfg.get("token_file")
    if not token and token_file:
        token_file = os.path.expanduser(token_file)
        if not os.path.isabs(token_file):  # relative paths sit next to config.json
            token_file = os.path.join(base, token_file)
        if os.path.exists(token_file):
            token = open(token_file).read().strip()
    if not token:
        raise Fatal("no GitLab token: put one in %s, or set $GITLAB_TOKEN"
                    % (token_file or "config.json"))
    cfg["token"] = token

    if not cfg.get("gitlab_url") or "example.com" in cfg["gitlab_url"]:
        raise Fatal("set gitlab_url in %s" % path)
    cfg["gitlab_url"] = cfg["gitlab_url"].rstrip("/")
    return cfg


def log(cfg, msg):
    line = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    path = cfg["log_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write("%s  %s\n" % (datetime.now().isoformat(timespec="seconds"), msg))


def notify(cfg, title, message):
    """macOS banner. Never let a failed notification fail a review."""
    if not cfg.get("notify"):
        return
    try:
        subprocess.run(
            ["osascript", "-e", "on run argv",
             "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
             "-e", "end run", message, title],
            capture_output=True, timeout=5)
    except Exception:
        pass


def load_state(cfg):
    return json.load(open(cfg["state_file"])) if os.path.exists(cfg["state_file"]) else {}


def save_state(cfg, state):
    with open(cfg["state_file"], "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# GitLab
# --------------------------------------------------------------------------- #

def api(cfg, method, path, params=None, form=None):
    url = cfg["gitlab_url"] + "/api/v4" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "PRIVATE-TOKEN": cfg["token"],
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise Fatal("GitLab rejected the token (%s): %s" % (exc.code, detail))
        raise RuntimeError("%s %s -> %s: %s" % (method, path, exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("GitLab unreachable: %s" % exc.reason)
    return json.loads(raw) if raw.strip() else None


def todo_mrs(cfg):
    return api(cfg, "GET", "/todos", params={
        "action": "review_requested", "type": "MergeRequest",
        "state": "pending", "per_page": 50,
    }) or []


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #

def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), proc.stderr.strip()[:300]))
    return proc.stdout.strip()


def find_repo(cfg, project_path):
    """group/sub/project -> local clone: from the config map, else by origin URL."""
    repo = os.path.expanduser(cfg["repos"].get(project_path, ""))
    if repo and os.path.isdir(repo):
        return repo

    for root in map(os.path.expanduser, cfg["search_roots"]):
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if not os.path.isdir(os.path.join(dirpath, ".git")):
                continue
            url = git(dirpath, "remote", "get-url", "origin", check=False).rstrip("/")
            url = url[:-4] if url.endswith(".git") else url
            if url.endswith("/" + project_path) or url.endswith(":" + project_path):
                log(cfg, "found %s at %s" % (project_path, dirpath))
                return dirpath

    raise Skip("no local clone for %s — add it to repos in config.json" % project_path)


def make_worktree(cfg, repo, iid, target):
    """Fetch the MR head and target branch; return (worktree, merge_base, head)."""
    ref = "refs/merge-requests/%s/head" % iid
    git(repo, "worktree", "prune", check=False)
    git(repo, "fetch", "--prune", "origin", "+%s:%s" % (ref, ref),
        "+refs/heads/{0}:refs/remotes/origin/{0}".format(target))

    head = git(repo, "rev-parse", ref)
    base = git(repo, "merge-base", "refs/remotes/origin/" + target, head)
    if not base:
        raise Skip("no merge base between %s and %s" % (target, ref))

    wt = os.path.join(cfg["workspace"], "mr-%s-%s" % (iid, head[:10]))
    git(repo, "worktree", "remove", "--force", wt, check=False)
    os.makedirs(cfg["workspace"], exist_ok=True)
    # Detached at the target tip: HEAD == origin/<target>, the state the skill
    # expects from a manual run. The MR is passed to it as an explicit ref.
    git(repo, "worktree", "add", "--detach", wt, "refs/remotes/origin/" + target)
    return wt, base, head


def drop_worktree(repo, iid, wt):
    git(repo, "worktree", "remove", "--force", wt, check=False)
    git(repo, "update-ref", "-d", "refs/merge-requests/%s/head" % iid, check=False)
    git(repo, "worktree", "prune", check=False)


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #

def run_skill(cfg, wt, values):
    prompt = open(cfg["prompt_file"]).read()
    for key, val in values.items():
        prompt = prompt.replace("{{%s}}" % key, str(val))

    proc = subprocess.run(
        [cfg["claude_bin"], "-p", prompt, "--output-format", "json",
         "--max-turns", str(cfg["max_turns"]),
         "--max-budget-usd", str(cfg["max_budget_usd"]),
         "--allowedTools", cfg["allowed_tools"]],
        cwd=wt, capture_output=True, text=True,
    )
    if proc.returncode:
        raise RuntimeError("claude exited %s: %s" % (proc.returncode, proc.stderr.strip()[:300]))

    out = json.loads(proc.stdout)
    if out.get("subtype") != "success" or not (out.get("result") or "").strip():
        raise RuntimeError("claude run failed (subtype=%s)" % out.get("subtype"))
    log(cfg, "  review: %s chars, %s turns, $%s"
        % (len(out["result"]), out.get("num_turns"), out.get("total_cost_usd")))
    return out["result"].strip()


def review_mr(cfg, state, todo, dry_run):
    project = todo["project"]
    iid = todo["target"]["iid"]
    pid, path = project["id"], project["path_with_namespace"]
    key = "%s:%s" % (pid, iid)

    mr = api(cfg, "GET", "/projects/%s/merge_requests/%s" % (pid, iid))
    if mr["state"] != "opened":
        raise Skip("MR is " + mr["state"])
    if cfg["skip_drafts"] and (mr.get("draft") or mr.get("work_in_progress")):
        raise Skip("MR is a draft")
    if state.get(key) == mr["sha"]:
        raise Skip("already reviewed %s" % mr["sha"][:10])

    repo = find_repo(cfg, path)
    ref = "refs/merge-requests/%s/head" % iid
    log(cfg, "!%s %s — %s → %s" % (iid, path, mr["title"][:50], mr["target_branch"]))
    wt, base, head = make_worktree(cfg, repo, iid, mr["target_branch"])
    try:
        body = run_skill(cfg, wt, {
            "iid": iid, "title": mr["title"], "project_path": path,
            "author": mr["author"]["username"], "target_branch": mr["target_branch"],
            "mr_ref": ref, "base_sha": base, "head_sha": head,
        })
        if dry_run:
            log(cfg, "  dry run, not posting:\n" + body)
            return
        note = api(cfg, "POST", "/projects/%s/merge_requests/%s/draft_notes" % (pid, iid),
                   form={"note": body})
        log(cfg, "  posted draft note %s" % (note or {}).get("id"))
        notify(cfg, "Draft review ready: !%s" % iid, "%s — %s" % (path, mr["title"][:60]))
        api(cfg, "POST", "/todos/%s/mark_as_done" % todo["id"])
        state[key] = head
        save_state(cfg, state)
    finally:
        drop_worktree(repo, iid, wt)


def one_pass(cfg, dry_run, only_mr=None):
    state = load_state(cfg)
    todos = todo_mrs(cfg)
    if only_mr:
        todos = [t for t in todos if t["target"]["iid"] == only_mr]
        if not todos:
            log(cfg, "no pending todo for !%s" % only_mr)
            return

    log(cfg, "%d pending review request(s)" % len(todos))
    for todo in todos:
        iid = todo["target"]["iid"]
        try:
            review_mr(cfg, state, todo, dry_run)
        except Skip as exc:
            log(cfg, "!%s skipped: %s" % (iid, exc))
        except Fatal:
            raise
        except Exception as exc:  # one bad MR must not stop the rest
            log(cfg, "!%s FAILED: %s" % (iid, exc))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="review, but post nothing")
    ap.add_argument("--mr", type=int, help="only this MR iid")
    args = ap.parse_args()

    try:
        cfg = load_config(os.path.expanduser(args.config))
    except Fatal as exc:
        print("mra: %s" % exc, file=sys.stderr)
        return 2

    # one run at a time, so overlapping ticks can't double-review
    os.makedirs(cfg["dir"], exist_ok=True)
    lock = open(os.path.join(cfg["dir"], ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(cfg, "another run is in progress, exiting")
        return 0

    try:
        while True:
            if args.once or args.mr:
                one_pass(cfg, args.dry_run, args.mr)
                return 0
            one_pass(cfg, args.dry_run)
            time.sleep(cfg["poll_seconds"])
    except KeyboardInterrupt:
        return 0
    except Fatal as exc:
        log(cfg, "fatal: %s" % exc)
        return 2
    except Exception as exc:
        log(cfg, "error: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
