"""Cut long pauses inside the chosen segments, turning each one into tighter jump cuts.

Usage: python tighten.py --workdir WORK [--config config.json] [--file selections.json] [--only v01 h02]
                         [--write]

A pause is a gap between two transcribed words longer than tighten.min_gap_sec. It is removed,
keeping tighten.pad_sec of air on both sides, unless the audio in it is louder than the clip's
speech by tighten.keep_loud_db (laughing or screaming is rarely transcribed). Silence before the
first and after the last word of a segment counts too. Segments without words stay as they are.
Notes that fall in a removed pause move to the start of the next piece, so their markers survive.

Without --write it only prints what would change. --write saves the file and keeps the previous
version next to it as <name>.before_tighten.json.
"""
import argparse
import shutil
from pathlib import Path

from common import fmt_ts, load_best_transcript, load_config, load_json, normalize_segments, parse_ts, save_json, work_paths


def load_energy(path):
    """{second: rms_db} from audio_energy.csv, or {} when it's missing."""
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        t, db = line.split(",")[:2]
        out[int(float(t))] = float(db)
    return out


def words_in(tsegs, a, b):
    ws = [w for s in tsegs for w in (s.get("words") or []) if a <= (w["start"] + w["end"]) / 2 <= b]
    return sorted(ws, key=lambda w: w["start"])


def loud(energy, a, b, speech_db, margin):
    """True when a whole second inside (a, b) is louder than the speech level by margin dB."""
    secs = [energy[t] for t in range(int(a) + 1, int(b)) if t in energy]
    return speech_db is not None and bool(secs) and max(secs) >= speech_db + margin


def speech_level(energy, words):
    secs = sorted({int(w["start"]) for w in words if int(w["start"]) in energy})
    if not secs:
        return None
    vals = sorted(energy[t] for t in secs)
    return vals[len(vals) // 2]


def pauses(a, b, words, gap, pad, energy, margin):
    """Spans of [a, b] to remove: gaps longer than `gap`, shrunk by `pad`, that aren't loud."""
    if not words:
        return []
    level = speech_level(energy, words)
    edges = [a] + [x for w in words for x in (w["start"], w["end"])] + [b]
    out = []
    for i in range(0, len(edges), 2):  # (end of speech, start of next speech) pairs
        x, y = edges[i], edges[i + 1]
        if y - x <= gap or loud(energy, x, y, level, margin):
            continue
        cut_a = x if i == 0 else x + pad
        cut_b = y if i + 2 == len(edges) else y - pad
        if cut_b - cut_a > 0.05:
            out.append((cut_a, cut_b))
    return out


def tighten_clip(clip, tsegs, energy, cfg):
    """-> (segments, notes, removed seconds). Each piece keeps its segment's shape and crop."""
    t = cfg.get("tighten", {})
    gap, pad, margin = t.get("min_gap_sec", 0.8), t.get("pad_sec", 0.2), t.get("keep_loud_db", 3)
    pieces, removed = [], 0.0
    for raw, s in zip(clip["segments"], normalize_segments(clip)):
        a, b = s["a"], s["b"]
        cuts = pauses(a, b, words_in(tsegs, a, b), gap, pad, energy, margin)
        start = a
        for x, y in cuts + [(b, b)]:
            if x - start > 0.05:
                pieces.append((raw, start, x))
            start = y
        removed += sum(y - x for x, y in cuts)
    if not pieces:
        return clip["segments"], clip.get("notes", []), 0.0

    def shaped(raw, x, y):
        tx, ty = fmt_ts(x, ms=True), fmt_ts(y, ms=True)
        return {**raw, "in": tx, "out": ty} if isinstance(raw, dict) else [tx, ty]

    notes = []
    for n in clip.get("notes", []):
        at = parse_ts(n["at"])
        if not any(x <= at <= y for _, x, y in pieces):
            later = [x for _, x, _ in pieces if x > at]
            if later:
                at = min(later)
        notes.append({**n, "at": fmt_ts(at, ms=True)} if at != parse_ts(n["at"]) else n)
    return [shaped(*p) for p in pieces], notes, removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--file", help="預設：WORK/selections.json")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--write", action="store_true", help="寫回檔案（原本的版本留在 .before_tighten.json）")
    args = ap.parse_args()
    paths = work_paths(args.workdir)
    path = Path(args.file or paths["selections"])
    sel = load_json(path)
    cfg = load_config(args.config)
    tsegs = load_best_transcript(paths)
    energy = load_energy(paths["audio"])
    if not energy:
        print("[提示] 沒有 audio_energy.csv，無法判斷停頓裡有沒有笑聲或尖叫，全部依逐字稿")
    for c in sel["clips"]:
        if args.only and c["id"] not in args.only:
            continue
        before = sum(s["b"] - s["a"] for s in normalize_segments(c))
        segs, notes, removed = tighten_clip(c, tsegs, energy, cfg)
        print(f"{c['id']}：{len(c['segments'])} 段 {before:.1f} 秒 → {len(segs)} 段 {before - removed:.1f} 秒（剪掉 {removed:.1f} 秒）")
        c["segments"], c["notes"] = segs, notes
    if args.write:
        shutil.copy2(path, path.with_name(path.stem + ".before_tighten.json"))
        save_json(sel, path)
        print(f"已寫回 {path}（原本的版本：{path.stem}.before_tighten.json）")
    else:
        print("只是預覽；確認後加 --write 寫回")


if __name__ == "__main__":
    main()
