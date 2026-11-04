"""Fail when a tracked file contains a private term (client or guest names, local paths...).

Usage: python tools/privacy_check.py [--terms references/private_terms.txt]

The term list is one term per line ("#" starts a comment) and is never committed: it holds the very
names this check keeps out of the public repository. Without it the check is skipped, so CI and
other people's clones still pass. Only files git tracks or would add (not ignored) are scanned.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS = ROOT / "references" / "private_terms.txt"

for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp950
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_terms(path):
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    return [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]


def repo_files():
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                       capture_output=True, check=True)
    return [ROOT / p for p in r.stdout.decode("utf-8").split("\0") if p]


def scan(files, terms):
    """-> [(file, line number, term)] for every hit; binary files are skipped."""
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        low = text.lower()
        for term in terms:
            if term.lower() in low:
                for n, line in enumerate(text.splitlines(), 1):
                    if term.lower() in line.lower():
                        hits.append((f, n, term))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", default=str(TERMS))
    args = ap.parse_args()
    if not Path(args.terms).is_file():
        print(f"隱私檢查：沒有 {Path(args.terms).name}，略過")
        return 0
    terms = load_terms(args.terms)
    hits = scan(repo_files(), terms)
    for f, n, term in hits:
        # don't print the term itself: the output may end up in a public CI log or issue
        print(f"[隱私] {f.relative_to(ROOT)}:{n} 含第 {terms.index(term) + 1} 個私人詞")
    print(f"隱私檢查：{len(terms)} 個詞，{'發現 ' + str(len(hits)) + ' 處' if hits else '沒有問題'}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
