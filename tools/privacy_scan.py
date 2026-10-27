#!/usr/bin/env python3
"""Strict privacy gate for this public repo / site. Exit code 1 on any hit that is not on the reviewed allowlist.

Scans:
  - every file that is (or would be) committed: `git ls-files --cached --others --exclude-standard`
  - optionally the built site directory (--site _site)
  - optionally the whole git history and commit metadata (--history): all blobs ever committed, author/committer
    names and emails, commit messages, and the configured remotes (no embedded credentials)

Patterns:
  - generic patterns below (emails, phone numbers, tokens/secrets, cookies, local paths, portfolio words)
  - private patterns (personal names, account numbers, ...) are NOT stored in this public file. They are read from the
    environment variable PRIVACY_PATTERNS (GitHub Actions secret) and/or the untracked file .privacy_patterns
    (one regex per line, case-insensitive). In CI the gate fails if no private patterns were provided.
"""
import os, re, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEB = "\u0590-\u05FF"

GENERIC = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "phone": r"(?<![\w.,])(?:\+972[- ]?|0)(?:5\d|[2-9])[- ]?\d{3}[- ]?\d{4}(?![\w.])|(?<![\w.,])\+\d{1,3}[ -]\(?\d{2,4}\)?[ -]\d{3,4}[ -]\d{3,4}(?![\w.])",
    "github_token": r"\b(?:gho|ghp|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    "secret_kv": r"(?i)\b(?:api[_-]?key|apikey|secret|password|passwd|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{8,}",
    "token_param": r"(?i)[?&](?:token|key|apikey|access_token|sig)=[A-Za-z0-9_\-]{8,}",
    "bearer": r"(?i)\bBearer\s+[A-Za-z0-9_\-.=]{10,}",
    "cookie": r"(?i)\b(?:set-cookie|cookie)\s*:\s*\S+=",
    "aws_key": r"\bAKIA[0-9A-Z]{16}\b",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
    "url_credentials": r"https?://[^/\s:@]+:[^/\s@]+@",
    "local_path": r"/workspace\b|/home/[a-z]|/cursor/|/tmp/|[A-Z]:\\\\Users\\\\|/Users/[A-Za-z]",
    "portfolio_en": r"(?i)portfolio|holdings_clean|ticker_map|\bmtm_|local_equiv|value_ils|my holdings|positions\.csv",
    "portfolio_he": rf"(?<![{HEB}])(?:[והבלמש]|של)?(?:תיק|התיק|תיקי|תיקים)(?:י|ך|נו)?(?![{HEB}])|אחזק|החזקות שלי|השקעות שלי",
    # anchored at the start of a number run (lookbehind) so long JSON number lists do not cause quadratic backtracking
    "shekel_amount": r"₪\s*\d|(?<![\d,.])\d[\d,.]{0,20}\s*₪|\bILS\s*\d[\d,]{3,}|(?<![\d,.])\d[\d,]{4,20}(?:\.\d+)?\s*(?:ILS|ש\"ח|שח)\b",
}

# Reviewed, legitimate occurrences: (pattern name, regex the matched text or its line must satisfy, files regex, reason)
ALLOW = [
    ("portfolio_he", r"(?:קובץ|קבצי|בקובץ|מקובץ|לקובץ)\s+(?:ה)?אחזקות|אחזקות\s+(?:iShares|Global X)|רכיבי הקרן|קובץ אחזקות|משקלי האחזקות", r".*",
     "fund holdings files published by iShares / Global X (public fund data), not personal holdings"),
    ("portfolio_he", r"אחזקות .{0,40}(?:iShares|Global X|הקרן|{fund}|\$\{)", r".*", "public fund holdings wording"),
    ("local_path", r"/tmp/", r"^\.github/workflows/.*|^tools/privacy_scan\.py$", "CI temp files"),
    ("email", r"github-actions\[bot\]@users\.noreply\.github\.com|elchananh@users\.noreply\.github\.com", r".*",
     "GitHub noreply addresses (bot / repo owner handle), no personal email"),
    ("email", r"@[0-9]+\.[0-9]+\.[0-9]+", r"^assets/plotly-basic\.min\.js$", "npm version strings inside the Plotly bundle"),
    ("token_param", r".*", r"^assets/plotly-basic\.min\.js$", "Plotly bundle code (no credentials)"),
    ("secret_kv", r"(?i)\b(?:api[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?(?:this|e|t|n|r|o|i|a|void|null|undefined|\w\.\w)", r"^assets/plotly-basic\.min\.js$", "Plotly bundle variable names"),
]
SELF_DEFINITIONS = r"^(?:history:)?tools/privacy_scan\.py$"   # this file defines the generic regexes; its own lines are checked separately

def load_private():
    pats = []
    env = os.environ.get("PRIVACY_PATTERNS", "")
    pats += [p.strip() for p in env.splitlines() if p.strip() and not p.strip().startswith("#")]
    f = os.path.join(ROOT, ".privacy_patterns")
    if os.path.exists(f):
        pats += [p.strip() for p in open(f, encoding="utf-8") if p.strip() and not p.strip().startswith("#")]
    return [re.compile(p, re.I) for p in dict.fromkeys(pats)]

def git(*a):
    return subprocess.run(["git", "-C", ROOT] + list(a), capture_output=True, text=True, errors="replace").stdout

def allowed(name, match, line, path):
    path = re.sub(r"^history:", "", path)
    for n, rx, frx, _ in ALLOW:
        if n == name and re.search(frx, path) and (re.search(rx, match) or re.search(rx, line)):
            return True
    return False

GENERIC_RX = {n: re.compile(r) for n, r in GENERIC.items()}

def scan_text(path, text, private, hits):
    """Whole-text scan with compiled regexes (fast on minified bundles / one-line JSON)."""
    def ln_of(pos):
        return text.count("\n", 0, pos) + 1
    for i, rx in enumerate(private):
        for m in rx.finditer(text):
            hits.append((path, ln_of(m.start()), f"private#{i+1}", m.group(0), text[max(0, m.start() - 50):m.end() + 50]))
    if re.search(SELF_DEFINITIONS, path):
        return
    for name, rx in GENERIC_RX.items():
        for m in rx.finditer(text):
            ctx = text[max(0, m.start() - 60):m.end() + 60]
            if not allowed(name, m.group(0), ctx, path):
                hits.append((path, ln_of(m.start()), name, m.group(0), ctx))

def read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="ignore")

def main():
    argv = sys.argv[1:]
    private = load_private()
    hits, scanned = [], 0
    in_ci = os.environ.get("CI") == "true"
    if not private:
        msg = "no private patterns provided (PRIVACY_PATTERNS secret / .privacy_patterns file)"
        if in_ci or "--require-private" in argv:
            print("PRIVACY GATE FAILED:", msg); return 1
        print("WARNING:", msg)
    files = [f for f in git("ls-files", "--cached", "--others", "--exclude-standard").splitlines() if f]
    if not files:   # not a git checkout: walk the folder
        for dp, dn, fn in os.walk(ROOT):
            dn[:] = [d for d in dn if d not in (".git", "screenshots", "__pycache__", "cache", "_site")]
            files += [os.path.relpath(os.path.join(dp, x), ROOT) for x in fn]
    for f in files:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            scan_text(f, read(p), private, hits); scanned += 1
            if f.startswith(".git/") or "/.git/" in f:
                hits.append((f, 0, "git_dir", f, "a .git path is tracked"))
    if "--site" in argv:
        site = argv[argv.index("--site") + 1]
        for dp, dn, fn in os.walk(site):
            for x in fn:
                p = os.path.join(dp, x); rel = "site:" + os.path.relpath(p, site)
                if ".git" in p.split(os.sep): hits.append((rel, 0, "git_dir", rel, ".git inside the published site"))
                scan_text(rel.replace("site:", ""), read(p), private, hits); scanned += 1
    if "--history" in argv:
        meta = git("log", "--all", "--format=%an <%ae> | %cn <%ce> | %s%n%b")
        scan_text("git:metadata", meta, private, hits)
        for line in meta.splitlines():
            for e in re.findall(GENERIC["email"], line):
                if e not in ("elchananh@users.noreply.github.com", "41898282+github-actions[bot]@users.noreply.github.com"):
                    hits.append(("git:metadata", 0, "author_email", e, line))
        remotes = git("remote", "-v")
        if re.search(r"https?://[^/\s]*@", remotes):
            hits.append(("git:remote", 0, "url_credentials", "credentials in remote URL", "(hidden)"))
        objs = git("rev-list", "--all", "--objects").splitlines()
        for o in objs:
            parts = o.split(" ", 1)
            if len(parts) == 2 and git("cat-file", "-t", parts[0]).strip() == "blob":
                blob = subprocess.run(["git", "-C", ROOT, "cat-file", "-p", parts[0]], capture_output=True).stdout.decode("utf-8", "ignore")
                scan_text("history:" + parts[1], blob, private, hits); scanned += 1
    # report (private matches are never echoed in full)
    seen = set()
    for path, ln, name, m, ctx in hits:
        key = (path, name, m)
        if key in seen: continue
        seen.add(key)
        shown = (m[:2] + "…") if name.startswith("private") or name in ("github_token", "bearer", "secret_kv", "url_credentials") else m
        print(f"HIT {name:14s} {path}:{ln}  {shown!r}")
    print(f"privacy scan: {scanned} files/blobs scanned, {len(private)} private patterns, {len(GENERIC)} generic patterns, "
          f"{len(seen)} unreviewed hits -> {'FAIL' if seen else 'PASS'}")
    return 1 if seen else 0

if __name__ == "__main__":
    sys.exit(main())
