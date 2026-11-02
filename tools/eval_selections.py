"""Measure picks against clips a person chose, to compare prompts, weights or models on a fixed baseline.

Usage:
  python tools/eval_selections.py --truth selections.json --pred selections.draft.json
  python tools/eval_selections.py --truth selections.json --candidates candidates.json

--pred reports, per chosen clip, how many of its seconds the picks cover (each second counted once,
so overlapping picks can't push it past 100%), plus the share of picked seconds that were chosen.
Length matters: more picks raise coverage, so always read coverage and precision together.
--candidates reports the best rank of a candidate peak inside each chosen clip (--pad seconds of slack).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from common import fmt_ts, load_json, merge_windows, normalize_segments  # noqa: E402


def intervals(clips):
    return merge_windows([(s["a"], s["b"]) for c in clips for s in normalize_segments(c)], 0, join_gap=0)


def covered(a, b):
    """Seconds of merged intervals a that lie inside merged intervals b."""
    return sum(max(0.0, min(x[1], y[1]) - max(x[0], y[0])) for x in a for y in b)


def eval_pred(truth, pred):
    picks = intervals(pred)
    rows = []
    for c in truth:
        mine = intervals([c])
        total = sum(b - a for a, b in mine)
        rows.append({"id": c["id"], "title": c.get("title", ""), "start": mine[0][0], "seconds": total,
                     "coverage": covered(mine, picks) / total if total else 0.0})
    truth_all = intervals(truth)
    pick_total = sum(b - a for a, b in picks)
    return {"rows": rows, "hit": sum(1 for r in rows if r["coverage"] > 0), "good": sum(1 for r in rows if r["coverage"] >= 0.5),
            "precision": covered(picks, truth_all) / pick_total if pick_total else 0.0,
            "pick_count": len(pred), "pick_seconds": pick_total}


def eval_candidates(truth, cands, pad=10):
    rows = []
    for c in truth:
        segs = normalize_segments(c)
        hits = [x for x in cands if any(s["a"] - pad <= x["peak"] <= s["b"] + pad for s in segs)]
        rows.append({"id": c["id"], "title": c.get("title", ""), "start": segs[0]["a"],
                     "best_rank": min((x["rank"] for x in hits), default=None), "hits": len(hits)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", required=True)
    ap.add_argument("--pred")
    ap.add_argument("--candidates")
    ap.add_argument("--pad", type=float, default=10)
    args = ap.parse_args()
    if not (args.pred or args.candidates):
        raise SystemExit("[錯誤] 要給 --pred 或 --candidates")
    truth = load_json(args.truth)["clips"]
    if args.pred:
        r = eval_pred(truth, load_json(args.pred)["clips"])
        for x in r["rows"]:
            print(f"{x['id']}  {fmt_ts(x['start'])}  {x['seconds']:6.0f} 秒  涵蓋 {x['coverage']:4.0%}  {x['title']}")
        print(f"\n有重疊 {r['hit']}/{len(truth)} 支；涵蓋過半 {r['good']}/{len(truth)} 支")
        print(f"推薦 {r['pick_count']} 支、共 {r['pick_seconds'] / 60:.1f} 分鐘，其中 {r['precision']:.0%} 落在人工選的片段內")
    if args.candidates:
        rows = eval_candidates(truth, load_json(args.candidates)["candidates"], args.pad)
        for x in rows:
            rank = f"第 {x['best_rank']} 名（{x['hits']} 個候選）" if x["best_rank"] else "沒有候選"
            print(f"{x['id']}  {fmt_ts(x['start'])}  {rank}  {x['title']}")
        print(f"\n前 20 名有 {sum(1 for x in rows if x['best_rank'] and x['best_rank'] <= 20)}/{len(rows)} 支")


if __name__ == "__main__":
    main()
