"""Check a selections file before rendering, and optionally snap cuts to sentence boundaries.

Usage: python validate_selections.py --workdir WORK [--config config.json]
                                     [--file selections.draft.json] [--snap]

Errors would make rendering fail or produce a wrong clip; warnings are for a person to judge (length
outside the limits in config "limits", overlapping segments, a cut in the middle of a sentence).
--snap rewrites the file with cuts moved to the nearest sentence or word boundary.
Exit code 1 when there are errors.
"""
import argparse
import re
import sys

from common import (CLIP_ID, fmt_ts, load_best_transcript, load_config, load_json, normalize_segments, probe, save_json,
                    work_paths)
from cut_clips import clip_stem

MID_MARGIN = 0.15  # a cut this close to a sentence edge doesn't count as cutting into it
PAUSE_OK = 0.3  # a cut in a pause between words at least this long doesn't cut into speech


def segment_at(segs, t):
    """The transcript sentence that t falls inside, if any."""
    for s in segs:
        if s["start"] + MID_MARGIN < t < s["end"] - MID_MARGIN:
            return s
    return None


def cuts_speech(segs, t):
    """True when a cut at t falls inside a sentence and not in a pause between its words."""
    s = segment_at(segs, t)
    if not s:
        return False
    ws = s.get("words") or []
    before = [w for w in ws if w["end"] <= t]
    after = [w for w in ws if w["start"] >= t]
    if not ws or len(before) + len(after) < len(ws):  # no word timing, or t splits a word
        return True
    if not before or not after:
        return False
    return after[0]["start"] - before[-1]["end"] < PAUSE_OK


def snap_range(a, b, segs, max_shift=1.5, pad=0.2):
    """Move a cut to its sentence edge when that is within max_shift seconds, else out of the word
    it splits. Cuts that aren't inside a sentence stay put."""
    def word_at(s, t):
        return next((w for w in s.get("words") or [] if w["start"] < t < w["end"]), None)

    new_a, new_b = a, b
    s = segment_at(segs, a)
    if s:
        w = word_at(s, a)
        if a - s["start"] <= max_shift:
            new_a = max(0.0, s["start"] - pad)
        elif w:
            new_a = max(0.0, w["start"] - 0.05)
    s = segment_at(segs, b)
    if s:
        w = word_at(s, b)
        if s["end"] - b <= max_shift:
            new_b = s["end"] + pad
        elif w:
            new_b = w["end"] + 0.05
    return (new_a, new_b) if new_a < new_b else (a, b)


def snap_clip(clip, segs, duration=None):
    """Copy of the clip's segments with snapped times, keeping each segment's shape and crop."""
    out = []
    for raw, s in zip(clip["segments"], normalize_segments(clip)):
        a, b = snap_range(s["a"], s["b"], segs)
        if duration:
            b = min(b, duration)
        a_txt, b_txt = fmt_ts(a, ms=True), fmt_ts(b, ms=True)
        if isinstance(raw, dict):
            out.append({**raw, "in": a_txt, "out": b_txt})
        else:
            out.append([a_txt, b_txt])
    return out


def validate(clips, cfg, duration=None, segs=()):
    """-> list of {"id", "level": "error" | "warning", "msg"}; every clip is checked."""
    issues = []
    limits = cfg.get("limits", {})
    presets = cfg.get("vertical_layout", {}).get("crop_presets", {})
    seen, stems = set(), {}

    for c in clips:
        cid = str(c.get("id", "?"))

        def add(level, msg, cid=cid):
            issues.append({"id": cid, "level": level, "msg": msg})

        if cid in seen:
            add("error", "片段 id 重複")
        seen.add(cid)
        if not re.fullmatch(CLIP_ID, cid):
            add("error", "片段 id 只能用英數字、底線、連字號（最多 32 字），例如 v01")
        else:
            stem = clip_stem(c)
            if stem in stems and stems[stem] != cid:
                add("error", f"和 {stems[stem]} 會輸出成同一個檔名，請改 id 或標題")
            stems.setdefault(stem, cid)
        fmt = c.get("format")
        if fmt not in ("vertical", "horizontal"):
            add("error", f"format 不正確：{fmt}")
        try:
            times = normalize_segments(c)
        except (KeyError, IndexError, TypeError, ValueError):
            add("error", "時間格式不正確")
            continue
        if not times:
            add("error", "沒有片段")
            continue
        for s in times:
            label = f"{fmt_ts(s['a'])}–{fmt_ts(s['b'])}"
            if not 0 <= s["a"] < s["b"]:
                add("error", f"{label} 的進出點不正確")
            elif duration and s["b"] > duration + 0.5:
                add("error", f"{label} 超出直播長度 {fmt_ts(duration)}")
            crop = s.get("crop")
            if fmt == "vertical" and isinstance(crop, str) and crop not in presets:
                add("error", f"裁切「{crop}」不存在，可用：{'、'.join(presets)}")
            if cuts_speech(segs, s["a"]):
                add("warning", f"進點 {fmt_ts(s['a'])} 切在句子中間")
            if cuts_speech(segs, s["b"]):
                add("warning", f"出點 {fmt_ts(s['b'])} 切在句子中間")
        ordered = sorted(times, key=lambda s: s["a"])
        if any(x["b"] > y["a"] for x, y in zip(ordered, ordered[1:])):
            add("warning", "片段之間有重疊，同一段畫面會出現兩次（倒敘開場可忽略）")
        lo_hi = limits.get(f"{fmt}_sec")
        length = sum(s["b"] - s["a"] for s in times)
        if lo_hi and not lo_hi[0] <= length <= lo_hi[1]:
            add("warning", f"長度 {length:.0f} 秒，不在建議的 {lo_hi[0]}–{lo_hi[1]} 秒內")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--file", help="預設：WORK/selections.json")
    ap.add_argument("--snap", action="store_true", help="把進出點對齊句子邊界，並寫回檔案")
    args = ap.parse_args()

    paths = work_paths(args.workdir)
    path = args.file or paths["selections"]
    sel = load_json(path)
    cfg = load_config(args.config)
    segs = load_best_transcript(paths)
    duration = None
    try:
        duration = probe(sel["source"])["duration"]
    except (KeyError, RuntimeError, SystemExit):
        print("[提示] 讀不到直播檔，略過「超出直播長度」的檢查")
    if args.snap:
        for c in sel["clips"]:
            c["segments"] = snap_clip(c, segs, duration)
        save_json(sel, path)
        print(f"已對齊句子邊界 → {path}")
    issues = validate(sel["clips"], cfg, duration, segs)
    for i in issues:
        print(f"{'❌' if i['level'] == 'error' else '⚠️ '} {i['id']}：{i['msg']}")
    if not issues:
        print("檢查通過")
    sys.exit(1 if any(i["level"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()
