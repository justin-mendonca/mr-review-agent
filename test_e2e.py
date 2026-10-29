#!/usr/bin/env python3
"""End-to-end harness for mra.py: stub GitLab API + fake claude + real git repos."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = tempfile.mkdtemp(prefix="mra-test-")
ORIGIN = os.path.join(ROOT, "origin", "widgets")
CLONE = os.path.join(ROOT, "src", "widgets")
STATE = {"drafts": [], "done": [], "claude_cwds": [], "claude_prompts": []}


def sh(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("FAILED %s\n%s\n%s" % (cmd, p.stdout, p.stderr))
    return p.stdout.strip()


def build_repos():
    os.makedirs(ORIGIN)
    sh(["git", "init", "-q", "-b", "main"], ORIGIN)
    sh(["git", "config", "user.email", "t@example.com"], ORIGIN)
    sh(["git", "config", "user.name", "t"], ORIGIN)
    open(os.path.join(ORIGIN, "app.py"), "w").write("def add(a, b):\n    return a + b\n")
    sh(["git", "add", "-A"], ORIGIN)
    sh(["git", "commit", "-qm", "init"], ORIGIN)
    main_sha = sh(["git", "rev-parse", "HEAD"], ORIGIN)

    # feature branch: 2 commits off main
    sh(["git", "checkout", "-q", "-b", "feature/login"], ORIGIN)
    open(os.path.join(ORIGIN, "app.py"), "a").write("\ndef login(u):\n    return u\n")
    sh(["git", "commit", "-qam", "add login"], ORIGIN)
    open(os.path.join(ORIGIN, "extra.py"), "w").write("X = 1\n")
    sh(["git", "add", "-A"], ORIGIN)
    sh(["git", "commit", "-qm", "add extra"], ORIGIN)
    head_sha = sh(["git", "rev-parse", "HEAD"], ORIGIN)

    # publish the MR ref + main, exactly like GitLab does
    sh(["git", "update-ref", "refs/merge-requests/7/head", head_sha], ORIGIN)
    sh(["git", "checkout", "-q", "main"], ORIGIN)

    os.makedirs(os.path.dirname(CLONE))
    sh(["git", "clone", "-q", ORIGIN, CLONE])
    # advance origin main after the clone so a stale local main would be caught
    open(os.path.join(ORIGIN, "newer.py"), "w").write("Y = 2\n")
    sh(["git", "add", "-A"], ORIGIN)
    sh(["git", "commit", "-qm", "newer main"], ORIGIN)
    only_main_sha = sh(["git", "rev-parse", "HEAD"], ORIGIN)

    # dirty up the clone's working tree to prove we never touch it
    open(os.path.join(CLONE, "app.py"), "a").write("\n# UNCOMMITTED LOCAL WORK\n")
    return main_sha, head_sha, only_main_sha


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/v4/todos":
            assert q.get("action") == ["review_requested"], q
            return self._send(200, [{
                "id": 99,
                "action_name": "review_requested",
                "target_type": "MergeRequest",
                "project": {"id": 1, "path_with_namespace": "acme/widgets"},
                "target": {"iid": 7, "title": "Add login"},
            }])
        if parsed.path == "/api/v4/projects/1/merge_requests/7":
            return self._send(200, {
                "iid": 7, "title": "Add login", "state": "opened", "draft": False,
                "sha": STATE["head_sha"], "target_branch": "main",
                "author": {"username": "octocat"},
            })
        return self._send(404, {"message": "404"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if parsed.path == "/api/v4/projects/1/merge_requests/7/draft_notes":
            STATE["drafts"].append(form.get("note", [""])[0])
            return self._send(201, {"id": 555})
        if parsed.path == "/api/v4/todos/99/mark_as_done":
            STATE["done"].append(99)
            return self._send(204, {})
        return self._send(404, {"message": "404"})


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
prompt = sys.argv[2]
cwd = os.getcwd()
head = subprocess.run(["git","rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
target = subprocess.run(["git","rev-parse","refs/remotes/origin/main"],capture_output=True,text=True).stdout.strip()
diff = subprocess.run(["git","diff","--stat","refs/merge-requests/7/head"],capture_output=True,text=True).stdout
record = {"cwd": cwd, "head": head, "target": target, "prompt": prompt, "diff": diff}
print(json.dumps({"subtype":"success","result":"## Review\n\nLooks fine.\n\n"+json.dumps(record),"num_turns":4,"total_cost_usd":0.12,"session_id":"s1"}))
'''


def main():
    main_sha, head_sha, only_main_sha = build_repos()
    STATE["head_sha"] = head_sha

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    fake = os.path.join(ROOT, "fake-claude")
    open(fake, "w").write(FAKE_CLAUDE)
    os.chmod(fake, 0o755)

    cfgdir = os.path.join(ROOT, "hermes")
    os.makedirs(cfgdir, exist_ok=True)
    cfg = {
        "gitlab_url": "http://127.0.0.1:%d" % port,
        "token": "test-token",
        "dir": cfgdir,
        "repos": {"acme/widgets": CLONE},
        "claude_bin": fake,
        "notify": False,   # this suite must never pop a real banner
        "skill": "/staff-engineer-review",
    }
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.md"), cfgdir)
    cfg["prompt_file"] = os.path.join(cfgdir, "prompt.md")
    cfgpath = os.path.join(cfgdir, "config.json")
    json.dump(cfg, open(cfgpath, "w"))

    mra = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mra.py")

    def run(*extra):
        p = subprocess.run([sys.executable, mra, "--config", cfgpath, "--once"] + list(extra),
                           capture_output=True, text=True)
        print("---- run %s (exit %s)\n%s%s" % (" ".join(extra) or "first", p.returncode, p.stdout, p.stderr))
        return p

    # 1. happy path
    p = run()
    assert p.returncode == 0, "run failed"
    assert len(STATE["drafts"]) == 1, "expected 1 draft note, got %d" % len(STATE["drafts"])
    assert STATE["done"] == [99], "todo not marked done: %s" % STATE["done"]

    body = STATE["drafts"][0]
    record = json.loads(body.split("\n\n", 2)[2])
    assert record["head"] == only_main_sha, (
        "HEAD was %s, expected newest origin/main %s" % (record["head"][:8], only_main_sha[:8]))
    assert os.path.realpath(record["cwd"]).startswith(os.path.realpath(os.path.join(cfgdir, "worktrees"))), record["cwd"]
    assert "/staff-engineer-review" in record["prompt"], "skill not first in prompt"
    assert "refs/merge-requests/7/head" in record["prompt"], "MR ref missing from prompt"
    assert record["diff"].strip(), "diff was empty"
    assert not os.path.isdir(record["cwd"]), "worktree not cleaned up"
    assert "# UNCOMMITTED LOCAL WORK" in open(os.path.join(CLONE, "app.py")).read(), \
        "the user's real working tree was modified!"

    # 2. dedupe: same SHA must not re-review
    p = run()
    assert len(STATE["drafts"]) == 1, "duplicate review posted"
    assert "already reviewed" in p.stdout, p.stdout

    # 3. new push on the MR -> re-review
    sh(["git", "checkout", "-q", "feature/login"], ORIGIN)
    open(os.path.join(ORIGIN, "app.py"), "a").write("\n# fixup\n")
    sh(["git", "commit", "-qam", "fixup"], ORIGIN)
    new_sha = sh(["git", "rev-parse", "HEAD"], ORIGIN)
    sh(["git", "update-ref", "refs/merge-requests/7/head", new_sha], ORIGIN)
    sh(["git", "checkout", "-q", "main"], ORIGIN)
    STATE["head_sha"] = new_sha
    p = run()
    assert len(STATE["drafts"]) == 2, "new revision was not re-reviewed"

    # 4. dry run reviews but posts nothing, and does not consume the review
    sh(["git", "checkout", "-q", "feature/login"], ORIGIN)
    open(os.path.join(ORIGIN, "extra.py"), "w").write("X = 2\n")
    sh(["git", "commit", "-qam", "bump"], ORIGIN)
    dry_sha = sh(["git", "rev-parse", "HEAD"], ORIGIN)
    sh(["git", "update-ref", "refs/merge-requests/7/head", dry_sha], ORIGIN)
    sh(["git", "checkout", "-q", "main"], ORIGIN)
    STATE["head_sha"] = dry_sha
    before = len(STATE["drafts"])
    p = run("--dry-run")
    assert len(STATE["drafts"]) == before, "--dry-run posted a note"
    assert "### Review" in p.stdout or "## Review" in p.stdout, "dry run produced no review text"

    # 5. the same revision is still reviewable for real afterwards
    p = run()
    assert len(STATE["drafts"]) == before + 1, "dry run wrongly consumed the review"

    # 6. notify() calls osascript with title + message (fake osascript, no banner)
    here = os.path.dirname(os.path.abspath(__file__))
    fakebin = os.path.join(ROOT, "bin")
    os.makedirs(fakebin, exist_ok=True)
    out = os.path.join(ROOT, "notified.txt")
    with open(os.path.join(fakebin, "osascript"), "w") as fh:
        fh.write('#!/bin/sh\n: > "$OUT"\nfor a in "$@"; do echo "$a" >> "$OUT"; done\n')
    os.chmod(os.path.join(fakebin, "osascript"), 0o755)
    env = dict(os.environ, PATH=fakebin + ":" + os.environ["PATH"], OUT=out)
    subprocess.run([sys.executable, "-c",
                    "import mra; mra.notify({'notify': True}, 'TITLE!', 'MSG!')"],
                   cwd=here, env=env, check=True)
    said = open(out).read()
    assert "TITLE!" in said and "MSG!" in said, said

    srv.shutdown()
    print("\nALL CHECKS PASSED  (root: %s)" % ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
