"""Suggest text_replacements by comparing machine subtitles with the editor's corrected ones.

Usage: python suggest_terms.py --original a.srt --corrected a_校正.srt [--original b.srt --corrected b_校正.srt]
                               [--min-count 2]

Prints a JSON snippet for config.json and changes nothing. Only fixes seen at least --min-count times
are suggested: a one-off fix is usually a mishearing that depends on context, and replacing it
everywhere would break other sentences.
"""
import argparse
import difflib
import json
import re
from collections import Counter

TIME_LINE = re.compile(r"\d+:\d+:\d+[,.]\d+\s*-->")
MAX_TERM = 12  # longer differences are rewritten sentences, not terms


def srt_text(path):
    """Subtitle text only, joined in order; cue numbers and times are dropped, since they differ."""
    with open(path, encoding="utf-8-sig") as f:
        lines = [x.strip() for x in f]
    return "\n".join(x for x in lines if x and not x.isdigit() and not TIME_LINE.match(x))


def differences(original, corrected):
    """(wrong, right) pairs from the two texts. Spaces and line breaks don't count, and each side
    grows to whole CJK neighbours only when it would otherwise be a single character."""
    a, b = re.sub(r"\s+", "", original), re.sub(r"\s+", "", corrected)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "replace":
            continue
        wrong, right = a[i1:i2], b[j1:j2]
        if len(wrong) == 1 and i1 > 0 and j1 > 0:  # 1 character is too easy to hit elsewhere
            wrong, right = a[i1 - 1:i2], b[j1 - 1:j2]
        if len(wrong) >= 2 and len(wrong) <= MAX_TERM and len(right) <= MAX_TERM:
            out.append((wrong, right))
    return out


def suggest(pairs_of_texts, min_count=2):
    counts = Counter()
    for original, corrected in pairs_of_texts:
        counts.update(differences(original, corrected))
    return {w: r for (w, r), n in counts.most_common() if n >= min_count}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--original", action="append", required=True)
    ap.add_argument("--corrected", action="append", required=True)
    ap.add_argument("--min-count", type=int, default=2)
    args = ap.parse_args()
    if len(args.original) != len(args.corrected):
        raise SystemExit("[錯誤] --original 和 --corrected 要成對")
    found = suggest([(srt_text(o), srt_text(c)) for o, c in zip(args.original, args.corrected)], args.min_count)
    if not found:
        print(f"沒有出現 {args.min_count} 次以上的固定修改")
        return
    print("確認過再把需要的項目加進 config.json：")
    print(json.dumps({"text_replacements": found}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
