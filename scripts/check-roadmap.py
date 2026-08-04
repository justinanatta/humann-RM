#!/usr/bin/env python3
"""Roadmap board checker — read-only. Python 3 stdlib only, no dependencies.

    python3 scripts/check-roadmap.py index.html
    python3 scripts/check-roadmap.py index.html --publish
    python3 scripts/check-roadmap.py index.html --date 2026-08-04
    python3 scripts/check-roadmap.py index.html --repo-only

Reports two independent categories:

  FAIL   — a broken invariant. The board is malformed or unsafe to publish.
           Exits 1.
  REVIEW — the encoding is valid but a date has passed, so a human must say
           what actually happened. A script cannot know whether work shipped
           or slipped, so it never guesses. Exits 2 (or 1 with --publish).

Never writes, reformats, or "fixes" anything.

Deliberately NOT checked: `prior`, `crosses`, and `--past` are derived at render
time (SPEC §2), so authored values are inert and checking them would only
generate noise.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser

# Three different version numbers live in this system; do not conflate them.
#   - The board's *content* revision is the visible "v1.4" in the topbar/footer.
#     It tracks what the PM changed and is checked separately below.
#   - The board's *format* version is <meta name="roadmap-format">. It says which
#     HTML encoding the board uses. It lives in the markup rather than the config
#     because it has to travel with the document and survive hand editing.
#   - The *toolkit release* is which bundle produced or last refreshed the repo.
# A checker declares which formats it understands. It deliberately does NOT warn
# merely because its own release is newer than the board: a repo-local checker
# cannot know whether a newer one exists, so that comparison is noise.
CHECKER_RELEASE = "2026.08.0"
SUPPORTED_FORMATS = {1}

PILLS = {"IMPL", "TEST", "BUG", "SPIKE", "CONV"}
LANES = {"cart", "plp", "experiments", "implementations", "compliance", "promotions"}
TERMINAL = {"done", "deferred"}
PLACEHOLDERS = ["SETUP:", "YOURORG", "Sample Client", "PROJ-"]

# --- the publish boundary is the repo, not the board ----------------------
# These deploys serve every committed file, so the board being clean says
# nothing about what the client can read. Two client boards served
# .claude/commands/*.md and roadmap.config.json publicly because they predate
# the scaffolder and never got a .vercelignore.
#
# The rule below is an ALLOWLIST: a committed path is acceptable only if it is
# the board, or the toolkit's own ignore rules certainly cover it, or a human
# named it in the config. A denylist has to be extended every time a repo grows
# a file and is silent when nobody remembers, which is the failure being closed.
REQUIRED_IGNORE = ["*.md", ".claude/", ".github/", "scripts/",
                   "roadmap.config.json", "roadmap.config.example.json",
                   ".roadmap/", ".gitignore"]
# Committed, not ignored, and verified not served: Vercel does not publish the
# ignore file itself (probed 404 on the one repo that commits it).
ALWAYS_ALLOWED = {".vercelignore"}
# A repo without these has no publish gate while looking completely fine.
REQUIRED_GATE = ["scripts/check-roadmap.py",
                 ".github/workflows/no-draft-on-main.yml"]


def _git(cwd, *args):
    """stdout of a git command, or None if git or the repo is unavailable."""
    try:
        p = subprocess.run(("git", "-C", cwd) + args, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
    except OSError:
        return None
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None


def _covered(path, rules):
    """True only if a rule the repo ACTUALLY HAS certainly matches this path.

    Deliberately narrower than gitignore semantics, and it consults only the
    rules present in this repo's .vercelignore. Being narrow costs a false FAIL
    a human clears in one line; being wide costs a silent green on a file the
    client can read. A rule the repo does not have cannot hide anything, so a
    missing ignore file names every committed path rather than one summary line.
    """
    parts = path.split("/")
    if "*.md" in rules and parts[-1].endswith(".md"):
        return True
    if parts[-1] in rules:                     # roadmap.config[.example].json
        return True
    for d in (".claude", ".github", "scripts", ".roadmap"):
        if d + "/" in rules and d in parts[:-1]:
            return True
    return False


def load_config(html):
    """(config dict, error string). Absent is {} plus no error; unparseable is
    {} plus an error, because a config that will not parse means live_url and
    deploy.allow are unknown and nothing below may be trusted."""
    p = os.path.join(os.path.dirname(os.path.abspath(html)), "roadmap.config.json")
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return {}, str(e)


def repo_checks(html, cfg, cfg_err):
    """Everything committed here that the client would be able to read."""
    fails = []
    board = os.path.abspath(html)
    top = _git(os.path.dirname(board), "rev-parse", "--show-toplevel")
    if not top:
        return ["repo: not a git repository, or git is unavailable. What gets "
                "published is every committed file, so a gate that cannot list "
                "them cannot certify anything."]
    root = top.strip()
    if cfg_err:
        fails.append(f"repo: roadmap.config.json will not parse ({cfg_err}), so "
                     f"live_url and deploy.allow are unknown.")

    ign = os.path.join(root, ".vercelignore")
    rules = set()
    if not os.path.exists(ign):
        fails.append("repo: no .vercelignore, so every committed file in this "
                     "repo is served publicly. Copy "
                     "~/.claude/skills/roadmap/assets/vercelignore here, or run "
                     "scaffold-roadmap.py . --upgrade.")
    else:
        with open(ign, encoding="utf-8") as f:
            rules = {ln.strip() for ln in f}
        miss = [r for r in REQUIRED_IGNORE if r not in rules]
        if miss:
            fails.append(f"repo: .vercelignore is missing required rule(s) "
                         f"{miss}. Run scaffold-roadmap.py . --upgrade.")
        rules &= set(REQUIRED_IGNORE)

    out = _git(root, "ls-files")
    if out is None:
        return fails + ["repo: `git ls-files` failed, so what is committed is "
                        "unknown. That is this check failing, not passing."]
    files = [f for f in out.splitlines() if f]

    allowed = {os.path.relpath(board, root).replace(os.sep, "/")} | ALWAYS_ALLOWED | \
        {str(x) for x in ((cfg.get("deploy") or {}).get("allow") or [])}
    for f in sorted(files):
        if f in allowed or _covered(f, rules):
            continue
        fails.append(f"repo: {f!r} is committed and no ignore rule in this repo "
                     f"covers it, so the client can read it at <live_url>/{f}. "
                     f"Delete it, ignore it, or list it in deploy.allow in "
                     f"roadmap.config.json once you have confirmed it is safe "
                     f"to publish.")

    for g in REQUIRED_GATE:
        if g not in files:
            fails.append(f"repo: {g} is not committed, so this repo has no "
                         f"publish gate. Run scaffold-roadmap.py . --upgrade.")

    remotes = (_git(root, "remote") or "").split()
    if remotes and not (cfg.get("live_url") or "").strip():
        fails.append(f"repo: live_url is empty but this repo has a remote "
                     f"({remotes[0]}), so it may be deployed and serving right "
                     f"now. An empty live_url is not evidence that nothing is "
                     f"public, it is the reason nobody looked. Fill it in, then "
                     f"run scripts/check-deploy.py.")
    return fails


class Board(HTMLParser):
    """Collect only what the checks need, with real tag/attribute parsing.

    Regex cannot be trusted here: the CI check this replaces matched only
    double-quoted single-line class attributes, so `class='group draft'` and a
    class attribute wrapped across lines both slipped past it silently.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.week_labels = []
        self.tracks = []          # cell count per row-track
        self.bars = []            # dict(classes, span, pills, label)
        self.bands = []           # (classes, span)
        self.milestones = []      # (classes, text)
        self.swatches = set()
        self.draft_lanes = []     # label text of each draft-tagged group
        self._track = None
        self._bar = None
        self._ms = None
        self._label = None

    @staticmethod
    def _classes(attrs):
        d = dict(attrs)
        return set((d.get("class") or "").split()), d

    @staticmethod
    def _span(style):
        m = re.search(r"grid-column:\s*(\d+)\s*/\s*(\d+)", style or "")
        return (int(m.group(1)), int(m.group(2))) if m else None

    def handle_starttag(self, tag, attrs):
        cls, d = self._classes(attrs)
        self.stack.append(cls)

        if tag == "details" and "draft" in cls:
            self.draft_lanes.append("")
            self._want_draft_name = True
        if "row-label" in cls and getattr(self, "_want_draft_name", False):
            self._label = "draftname"
            self._want_draft_name = False
        if "week-label" in cls:
            self.week_labels.append(cls)
        if "row-track" in cls:
            self._track = 0
        if "cell" in cls and self._track is not None:
            self._track += 1
        if "sprint-band" in cls:
            self.bands.append((cls, self._span(d.get("style"))))
        if "bar" in cls:
            self._bar = {"classes": cls, "span": self._span(d.get("style")),
                         "pills": [], "label": ""}
            self.bars.append(self._bar)
        if "bar-type" in cls and self._bar is not None:
            self._label = "pill"
        if "bar-label" in cls and self._bar is not None:
            self._label = "label"
        if "milestone" in cls:
            self._ms = {"classes": cls, "text": ""}
            self.milestones.append(self._ms)
        if "swatch" in cls:
            self.swatches |= cls

    def handle_endtag(self, tag):
        if self.stack:
            cls = self.stack.pop()
            if "row-track" in cls and self._track is not None:
                self.tracks.append(self._track)
                self._track = None
            if "bar" in cls:
                self._bar = None
            if "milestone" in cls:
                self._ms = None
        self._label = None

    def handle_data(self, data):
        t = data.strip()
        if not t:
            return
        if self._label == "pill" and self._bar is not None:
            self._bar["pills"].append(t)
        elif self._label == "label" and self._bar is not None:
            self._bar["label"] += t
        elif self._label == "draftname" and self.draft_lanes:
            self.draft_lanes[-1] = t
            self._label = None
        if self._ms is not None:
            self._ms["text"] += " " + t


def parse_dates(src):
    def grab(name):
        m = re.search(name + r"\s*=\s*new Date\('(\d{4})-(\d{2})-(\d{2})", src)
        return datetime.date(*map(int, m.groups())) if m else None
    return grab("ganttStart"), grab("ganttEnd")


def month_day_to_date(txt, start, end):
    """Resolve a bare 'M/D' milestone label to a date inside the board window."""
    m = re.search(r"(\d{1,2})/(\d{1,2})", txt)
    if not m:
        return None
    mo, day = int(m.group(1)), int(m.group(2))
    for yr in (start.year, start.year + 1):
        try:
            d = datetime.date(yr, mo, day)
        except ValueError:
            continue
        if start - datetime.timedelta(days=14) <= d <= end + datetime.timedelta(days=14):
            return d
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--date", help="evaluate as of YYYY-MM-DD (default: today)")
    ap.add_argument("--publish", action="store_true",
                    help="client-facing gate: draft lanes and placeholders become FAIL, "
                         "and REVIEW items block")
    ap.add_argument("--repo-only", action="store_true",
                    help="check only what is committed and publishable; skip the "
                         "board checks (use on an existing repo, where a stale "
                         "board would bury the finding under REVIEW noise)")
    a = ap.parse_args()

    cfg, cfg_err = load_config(a.html)
    if a.repo_only:
        rf = repo_checks(a.html, cfg, cfg_err)
        for f in rf:
            print(f"  FAIL    {f}")
        if not rf:
            print("  OK      only the board is publishable from this repo")
        print(f"\n{len(rf)} failure(s)")
        return 1 if rf else 0

    today = (datetime.date.fromisoformat(a.date) if a.date
             else datetime.date.today())
    # Fail closed. The check this replaces ran `grep ... index.html` and, when
    # the file was absent or renamed, grep exited 2, the `if` was false, and the
    # job reported "client-clean" and passed. A missing board is a failure.
    try:
        src = open(a.html, encoding="utf-8").read()
    except OSError as e:
        print(f"  FAIL    cannot read board file {a.html!r}: {e}")
        print("\n1 failure(s), 0 item(s) needing review")
        return 1

    b = Board()
    b.feed(src)

    fails, reviews = [], []
    start, end = parse_dates(src)
    ncols = len(b.week_labels)

    # --- 1. publication safety -------------------------------------------
    if b.draft_lanes:
        msg = f"{len(b.draft_lanes)} draft lane(s) present: " + \
              ", ".join(repr(x or "?") for x in b.draft_lanes)
        (fails if a.publish else reviews).append(
            msg + (" — must be promoted or dropped before publish" if a.publish
                   else " (fine on the review branch)"))
    if a.publish:
        fails += repo_checks(a.html, cfg, cfg_err)
        # A real client whose Jira key happens to be PROJ would otherwise fail
        # this gate forever, so consult the config before calling PROJ- a
        # placeholder. Everything else is unambiguous template text.
        real_key = (cfg.get("jira") or {}).get("project") or ""
        for p in PLACEHOLDERS:
            if p == "PROJ-" and real_key.upper() == "PROJ":
                continue
            if p in src:
                fails.append(f"unresolved template placeholder {p!r} still in the board")

    # --- 2. timeline consistency -----------------------------------------
    if not ncols:
        fails.append("no .week-label elements — cannot determine the column count")
    if start and end and ncols:
        expect = 7 * ncols
        got = (end - start).days
        if got != expect:
            fails.append(
                f"ganttEnd − ganttStart is {got} days but {ncols} columns need "
                f"{expect} (ganttEnd is the EXCLUSIVE Monday after the last column, "
                f"i.e. {start + datetime.timedelta(days=expect)})")
    elif not (start and end):
        fails.append("could not read ganttStart/ganttEnd from the script block")

    odd = [n for n in b.tracks if n and n != ncols]
    if odd and ncols:
        fails.append(f"{len(odd)} row-track(s) have a filler run of {sorted(set(odd))} "
                     f"cells instead of {ncols}")

    # --- 3. bar encoding --------------------------------------------------
    # A board with no bars is not a clean board, it is a broken one. Without
    # this, malformed markup that hides every lane (an unclosed comment, a
    # botched edit) passes every remaining check and reports OK, because each
    # one is vacuously true over an empty set.
    if not b.bars:
        fails.append("no bars found at all — the board is empty or its markup is "
                     "malformed (an unclosed comment or a broken tag will do it). "
                     "Open it in a browser before trusting any other result here.")
    if ncols and not b.tracks:
        fails.append("no .row-track elements found — the swimlanes are not being "
                     "parsed, so nothing below could be checked")

    for bar in b.bars:
        who = bar["label"] or "/".join(map(str, bar["span"] or [])) or "?"
        if bar["span"]:
            s, e = bar["span"]
            if not (1 <= s < e <= ncols + 1) and ncols:
                fails.append(f"bar {who!r}: grid-column {s}/{e} outside 1..{ncols + 1} "
                             f"or start >= end")
        else:
            fails.append(f"bar {who!r}: no parsable grid-column")
        pills = [p for p in bar["pills"] if p in PILLS]
        if "epic" not in bar["classes"] and len(pills) != 1:
            fails.append(f"bar {who!r}: expected exactly 1 task-type pill, found "
                         f"{bar['pills'] or 'none'}")
        if len(bar["classes"] & TERMINAL) > 1:
            fails.append(f"bar {who!r}: mutually exclusive states "
                         f"{sorted(bar['classes'] & TERMINAL)}")

    # --- 4. legend coverage ----------------------------------------------
    used_lanes = set().union(*[bar["classes"] & LANES for bar in b.bars]) if b.bars else set()
    for lane in sorted(used_lanes - b.swatches):
        fails.append(f"lane {lane!r} is used on a bar but has no legend swatch")
    if any("client-action" in bar["classes"] for bar in b.bars) and \
            "client-action-swatch" not in b.swatches:
        fails.append("client-action is used but the legend has no "
                     "'swatch client-action-swatch' entry (SPEC §3)")

    # --- 5. version agreement --------------------------------------------
    # Scoped to the topbar and footer only: milestone labels are release
    # versions (v2.4, v2.5, ...) and must not be mistaken for the format
    # version, or every board reports a spurious failure.
    vers = []
    for pat in (r'class="topbar-meta"[^>]*>(.*?)</div>', r"<footer[^>]*>(.*?)</footer>"):
        m = re.search(pat, src, re.S)
        if m:
            vers += re.findall(r"\bv(\d+\.\d+)\b", m.group(1))
    if len(set(vers)) > 1:
        fails.append(f"topbar/footer version strings disagree: {sorted(set(vers))} "
                     f"(SPEC §7 requires them to match)")

    # --- 5b. format compatibility ----------------------------------------
    fm = re.search(r'<meta[^>]+name=["\']roadmap-format["\'][^>]+content=["\'](\d+)["\']', src)
    if not fm:
        reviews.append(
            "board carries no <meta name=\"roadmap-format\"> stamp, so its encoding "
            "version is unknown (a board scaffolded before the toolkit added stamps). "
            "Add the meta tag from the current template when you next touch it.")
    else:
        fmt = int(fm.group(1))
        if fmt > max(SUPPORTED_FORMATS):
            fails.append(
                f"board declares roadmap-format {fmt}, but this checker "
                f"(release {CHECKER_RELEASE}) only understands "
                f"{sorted(SUPPORTED_FORMATS)}. Upgrade the toolkit in this repo "
                f"rather than trusting this result: an old checker cannot judge a "
                f"newer format and would pass markup it does not understand.")
        elif fmt not in SUPPORTED_FORMATS:
            fails.append(
                f"board declares roadmap-format {fmt}, which checker release "
                f"{CHECKER_RELEASE} no longer supports. Run the toolkit upgrade.")

    # --- 6. freshness — needs a human ------------------------------------
    if start and end and ncols:
        if today >= end:
            reviews.append(f"board window ended {end} — quarter roll overdue")
        line = 1 + max(0.0, min(1.0, (today - start).days / (7 * ncols))) * ncols

        for bar in b.bars:
            if not bar["span"] or bar["classes"] & TERMINAL or "epic" in bar["classes"]:
                continue
            if "proposed" in bar["classes"]:
                continue
            s, e = bar["span"]
            if e <= line:
                ended = start + datetime.timedelta(days=7 * (e - 1))
                reviews.append(f"in-flight bar {bar['label'] or f'{s}/{e}'!r} was scheduled "
                               f"to end {ended} — shipped, slipped, or re-scoped?")

        for ms in b.milestones:
            if "projected" not in ms["classes"]:
                continue
            d = month_day_to_date(ms["text"], start, end)
            if d and d < today:
                reviews.append(f"milestone {ms['text'].strip()!r} is still marked "
                               f"projected but {d} has passed")

    # --- report -----------------------------------------------------------
    print(f"check-roadmap {a.html}  (as of {today}"
          f"{', publish gate' if a.publish else ''})")
    print(f"  {ncols} columns · {len(b.tracks)} row-tracks · {len(b.bars)} bars · "
          f"{len(b.milestones)} milestones")
    print()
    for f in fails:
        print(f"  FAIL    {f}")
    for r in reviews:
        print(f"  REVIEW  {r}")
    if not fails and not reviews:
        print("  OK      all invariants hold and nothing has gone stale")
    print()
    print(f"{len(fails)} failure(s), {len(reviews)} item(s) needing review")

    if fails:
        return 1
    if reviews:
        return 1 if a.publish else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
