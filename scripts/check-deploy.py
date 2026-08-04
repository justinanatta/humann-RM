#!/usr/bin/env python3
"""Deploy probe — read-only. Python 3 stdlib only, no dependencies.

    python3 scripts/check-deploy.py            # uses roadmap.config.json
    python3 scripts/check-deploy.py index.html

Answers one question: can the client read anything in this repo other than the
board? It asks the live deploy rather than reasoning about ignore rules, because
the deploy is the only authority on what it serves.

Three things make this different from a single curl:

1. It needs a POSITIVE CONTROL. A 404 on a path only means "ignored" if the
   same origin returns 200 for the board. Without that, a wrong URL, a dead
   project or an unfinished deploy all read as a clean bill of health.
2. It derives the path list from what is COMMITTED on the deployed branch. A
   404 on a file that was never committed is indistinguishable from a working
   ignore rule, so a hardcoded list quietly certifies nothing.
3. An empty live_url is not evidence. If the repo has a remote it may well be
   deployed, so the absence of a URL is a failure to fix, not a step to skip.

Exit 0 clean, 1 leak or uninterpretable, 2 protected deploy (needs a human).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ALWAYS_ALLOWED = {".vercelignore"}
TIMEOUT = 15


def git(repo, *args):
    try:
        p = subprocess.run(("git", "-C", repo) + args,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def gits(repo, *args):
    out = git(repo, *args)
    return out.decode("utf-8", "replace") if out is not None else None


def fetch(url, body=False):
    """(status, bytes) — never raises. status None means the request failed."""
    req = urllib.request.Request(url, method="GET" if body else "HEAD",
                                 headers={"User-Agent": "roadmap-check-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, (r.read() if body else b"")
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:                        # DNS, TLS, timeout, refused
        return None, str(e).encode()


def host_repo_url(remote):
    """The GitHub API URL for a remote, or None if it is not GitHub."""
    r = remote.strip()
    if r.startswith("git@github.com:"):
        slug = r.split(":", 1)[1]
    elif "github.com/" in r:
        slug = r.split("github.com/", 1)[1]
    else:
        return None
    return "https://api.github.com/repos/" + slug[:-4] if slug.endswith(".git") \
        else "https://api.github.com/repos/" + slug


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html", nargs="?", help="board file (default: config html_file)")
    ap.add_argument("--branch", default="main",
                    help="the branch the client-facing deploy tracks (default: main)")
    a = ap.parse_args()

    start = os.path.abspath(a.html) if a.html else os.getcwd()
    top = gits(os.path.dirname(start) if a.html else start,
               "rev-parse", "--show-toplevel")
    if not top:
        print("  FAIL    not a git repository, or git is unavailable. This probe "
              "derives its path list from what is committed, so it cannot run "
              "here.")
        return 1
    root = top.strip()

    cfg = {}
    cfg_path = os.path.join(root, "roadmap.config.json")
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
        except ValueError as e:
            print(f"  FAIL    roadmap.config.json will not parse: {e}")
            return 1
    else:
        print("  FAIL    no roadmap.config.json in the repo root — nothing to "
              "read live_url from. Do not guess the URL.")
        return 1

    board = os.path.relpath(os.path.abspath(a.html), root).replace(os.sep, "/") \
        if a.html else (cfg.get("html_file") or "index.html")
    live = (cfg.get("live_url") or "").strip()
    remotes = (gits(root, "remote") or "").split()

    fails, notes = [], []

    # --- the empty-URL fail-open ------------------------------------------
    if not live:
        if remotes:
            print("  FAIL    live_url is empty, but this repo has a remote "
                  f"({remotes[0]}) so it may be deployed and serving right now. "
                  "An empty live_url is not evidence that nothing is public — it "
                  "is the reason nobody looked. Put the deployed URL in "
                  "roadmap.config.json and run this again.")
            return 1
        print("  OK      live_url is empty and this repo has no remote, so there "
              "is nothing deployed to probe. Fill live_url in the same commit "
              "that connects the deploy.")
        return 0

    origin = live.rstrip("/")

    # --- 1. positive control ----------------------------------------------
    code, body = fetch(f"{origin}/{board}", body=True)
    if code in (401, 403):
        print(f"  NOTE    {origin}/{board} returns {code}: Vercel Deployment "
              "Protection is on. This probe cannot see past the auth wall, so it "
              "can neither confirm nor deny a leak. Treat the repo-hygiene check "
              "in check-roadmap.py --publish as the only evidence you have.")
        return 2
    if code != 200:
        print(f"  FAIL    positive control failed: {origin}/{board} returned "
              f"{code}. Every 404 below would be meaningless, so this probe "
              f"proves nothing. Fix live_url or the deploy first.")
        return 1

    # --- 1b. is the deploy actually serving the branch we are listing? ----
    blob = git(root, "show", f"{a.branch}:{board}")
    if blob is None:
        blob = git(root, "show", f"origin/{a.branch}:{board}")
    # A mismatch is a FAIL, not a note. If the live deploy is not serving the
    # tree we are about to list, every 404 below may simply be a file the
    # deploy predates, and "I could not interpret this" must never print OK.
    if blob is None:
        fails.append(f"could not read {a.branch}:{board} from git, so there is no "
                     f"way to tell whether the live deploy serves this branch.")
    elif hashlib.sha256(blob).hexdigest() != hashlib.sha256(body).hexdigest():
        fails.append(f"the served board differs from {a.branch}:{board}, so this "
                     f"deploy is not serving the tree listed below and a 404 here "
                     f"proves nothing — it may just be a file the live deploy "
                     f"predates. Redeploy, or point --branch at what is actually "
                     f"deployed, then run this again.")

    # --- 2. one probe per committed path ----------------------------------
    tree = gits(root, "ls-tree", "-r", "--name-only", a.branch) or \
        gits(root, "ls-tree", "-r", "--name-only", f"origin/{a.branch}")
    if tree is None:
        print(f"  FAIL    cannot list branch {a.branch!r}, so there is no probe "
              f"list. This is the check failing, not passing.")
        return 1

    allowed = {board} | ALWAYS_ALLOWED | \
        {str(p) for p in ((cfg.get("deploy") or {}).get("allow") or [])}
    paths = [p for p in tree.split() if p not in allowed]

    print(f"check-deploy {origin}  (board {board} · branch {a.branch} · "
          f"{len(paths)} committed path(s) to probe)")
    for p in paths:
        code, _ = fetch(f"{origin}/{p}")
        mark = "LEAK" if code == 200 else ("?" if code is None else "ok")
        print(f"  {mark:<4}    {code}  /{p}")
        if code == 200:
            fails.append(f"{origin}/{p} is public. Anything committed to this "
                         f"repo is client-readable.")
        elif code is None:
            fails.append(f"could not reach {origin}/{p}, so it is unproven. "
                         f"Unproven is not clean.")

    # --- 3. the git host ---------------------------------------------------
    host_checked = False
    if remotes:
        api = host_repo_url(gits(root, "remote", "get-url", remotes[0]) or "")
        if api:
            host_checked = True
            code, _ = fetch(api)
            if code == 200:
                fails.append(f"the git host repo is readable without "
                             f"authentication ({api} returns 200). Ignore rules "
                             f"keep files off the page and do nothing about the "
                             f"repo: every file and every commit message, "
                             f"including anything you later deleted, is public. "
                             f"Make the repo private.")
            elif code not in (404, 401, 403):
                notes.append(f"could not establish whether the git host repo is "
                             f"public ({api} returned {code}).")

    print()
    for f in fails:
        print(f"  FAIL    {f}")
    for n in notes:
        print(f"  NOTE    {n}")
    if not fails:
        print(f"  OK      {len(paths)} committed path(s) probed, only the board is "
              f"served" + (", and the git host repo is not readable "
                           "unauthenticated." if host_checked else
                           ". The git host was NOT checked (no GitHub remote): "
                           "confirm by hand that the repo is private."))
    print()
    print(f"{len(fails)} failure(s), {len(notes)} note(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
