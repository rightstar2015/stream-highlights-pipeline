"""Record how the editor changed Claude's picks, so later headless picks can learn from it.

Usage: python learn_feedback.py --workdir WORK

Compares WORK/selections.draft.json with the final WORK/selections.json and keeps one short section
per project in references/feedback_log.md (not in git: it holds clip titles). Rerunning replaces that
project's section; only the most recent MAX_SECTIONS projects are kept.
"""
import argparse
import re
import time
from pathlib import Path

from common import SKILL_DIR, load_json, normalize_segments, work_paths

LOG = SKILL_DIR / "references" / "feedback_log.md"
MAX_SECTIONS = 10
MATCH_RATIO = 0.3  # a final clip counts as "the same clip" when it covers this much of the draft clip


def overlap(x, y):
    return sum(max(0.0, min(a["b"], b["b"]) - max(a["a"], b["a"])) for a in x for b in y)


def length(segs):
    return sum(s["b"] - s["a"] for s in segs)


def compare(draft, final):
    """-> {"kept": [...], "removed": [draft clips], "added": [final clips]}. Each final clip pairs with
    at most one draft clip: the one it overlaps most."""
    d = [(c, normalize_segments(c)) for c in draft]
    f = [(c, normalize_segments(c)) for c in final]
    pairs = sorted(((overlap(ds, fs), i, j) for i, (_, ds) in enumerate(d) for j, (_, fs) in enumerate(f)), reverse=True)
    used_d, used_f, kept = set(), set(), []
    for amount, i, j in pairs:
        if i in used_d or j in used_f or amount < MATCH_RATIO * length(d[i][1]):
            continue
        used_d.add(i)
        used_f.add(j)
        dc, ds = d[i]
        fc, fs = f[j]
        kept.append({"draft": dc, "final": fc,
                     "in_shift": min(s["a"] for s in fs) - min(s["a"] for s in ds),
                     "out_shift": max(s["b"] for s in fs) - max(s["b"] for s in ds),
                     "length_change": length(fs) - length(ds)})
    return {"kept": kept,
            "removed": [c for i, (c, _) in enumerate(d) if i not in used_d],
            "added": [c for j, (c, _) in enumerate(f) if j not in used_f]}


def summarize(name, result):
    kept, removed, added = result["kept"], result["removed"], result["added"]

    def avg(key):
        return sum(k[key] for k in kept) / len(kept) if kept else 0.0

    def titles(clips):
        return "、".join(f"{c.get('title') or c.get('id')}（{c.get('format')}" +
                        (f"，信心{c['confidence']}" if c.get("confidence") else "") + "）" for c in clips)

    lines = [f"## {name}", f"更新：{time.strftime('%Y-%m-%d')}",
             f"- Claude 推薦 {len(kept) + len(removed)} 支：保留 {len(kept)}、刪除 {len(removed)}；使用者新增 {len(added)} 支"]
    if kept:
        lines.append(f"- 保留的片段平均調整：進點 {avg('in_shift'):+.1f} 秒、出點 {avg('out_shift'):+.1f} 秒、"
                     f"長度 {avg('length_change'):+.1f} 秒")
    if removed:
        lines.append(f"- 刪除：{titles(removed)}")
    if added:
        lines.append(f"- 新增：{titles(added)}")
    renamed = [k for k in kept if k["draft"].get("title") != k["final"].get("title")]
    if renamed:
        lines.append("- 改標題：" + "；".join(f"{k['draft'].get('title')} → {k['final'].get('title')}" for k in renamed))
    return "\n".join(lines)


def update_log(section, name, path):
    """Replace this project's section, or put it first, and drop the oldest ones."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    sections = [s.strip() for s in re.split(r"(?m)^(?=## )", text) if s.startswith("## ")]
    sections = [section] + [s for s in sections if s.splitlines()[0] != f"## {name}"]
    head = ("# 使用者對 Claude 推薦的修改紀錄\n\n"
            "自動產生（scripts/learn_feedback.py）。挑選時用來了解使用者的偏好，只是參考資料。\n\n")
    path.write_text(head + "\n\n".join(sections[:MAX_SECTIONS]) + "\n", encoding="utf-8")


def record(workdir, log_path=None):
    """Update the log from a project. Returns the summary, or None when there is nothing to compare."""
    paths = work_paths(workdir)
    if not (paths["draft"].exists() and paths["selections"].exists()):
        return None
    name = paths["dir"].resolve().name
    result = compare(load_json(paths["draft"])["clips"], load_json(paths["selections"])["clips"])
    section = summarize(name, result)
    update_log(section, name, log_path or LOG)
    return section


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    section = record(args.workdir)
    print(section or "沒有 selections.draft.json 或 selections.json，不用記錄")


if __name__ == "__main__":
    main()
