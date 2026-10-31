"""Render approved highlights to MP4 + SRT.

Usage: python cut_clips.py --workdir WORK [--config config.json] [--only v01 h02] [--preview]

Reads WORK/selections.json:
{
  "source": "D:/直播/2026-10-25.mp4",
  "clips": [
    {"id": "v01", "format": "vertical", "title": "王也打不過", "header_title": "我破防了",
     "segments": [{"in": "1:02:03.5", "out": "1:02:12", "crop": "face"},
                  {"in": "1:02:20",   "out": "1:02:41", "crop": "game"}],
     "notes": [{"at": "1:02:07", "text": "插入驚嚇卡"},
               {"at": "1:02:30", "text": "放大臉"}]},
    {"id": "h01", "format": "horizontal", "title": "...",
     "segments": [[3600, 3660], [3672.5, 3790]]}
  ]
}
- segments: source-time ranges joined in order (jump cuts). [in, out] or {"in","out","crop"}.
- crop (vertical, template mode): a name from vertical_layout.crop_presets or [x, y, w, h].
- notes: editing suggestions; become markers in the Premiere XML (export_premiere_xml.py).
- header_title: optional title text for the Short's header banner (noted in manifest).
- Optional per clip "layout": {...} overrides vertical_layout keys.

Writes WORK/clips/<id>_<title>.mp4 and .srt, plus clips/manifest.json.
--preview renders one still per segment (fast check of crops / layout).
"""
import argparse
import math
import re
import shutil
import tempfile
from pathlib import Path

from common import (SKILL_DIR, fmt_ts, load_best_transcript, load_config, load_json, need_tool,
                    normalize_segments, parse_ts, probe, run, save_json,
                    source_to_clip_time, work_paths)

PUNCT_END = "。！？!?，,、；;：:…~～"
PUNCT_SOFT = "，,、；;：:。"


def even(x):
    return int(round(x / 2.0)) * 2


def clamp_crop(c, sw, sh):
    x, y, w, h = [int(v) for v in c]
    x, y = max(0, min(x, sw - 2)), max(0, min(y, sh - 2))
    w, h = min(w, sw - x), min(h, sh - y)
    return even(w), even(h), x, y


def resolve_template(lay):
    p = (lay.get("template_png") or "").strip()
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = SKILL_DIR / path
    return path if path.exists() else None


def vertical_filter(lay, sw, sh, crop=None, has_template=False):
    """Return a filter_complex producing [out]. Input 0 = stream, input 1 = template PNG (optional)."""
    W, H = lay["output_size"]
    mode = lay.get("mode", "template")
    if mode == "template":
        X, Y, CW, CH = lay["content_box"]
        presets = lay.get("crop_presets", {})
        name = crop or lay.get("default_crop", "game")
        region = name if isinstance(name, (list, dict)) else presets.get(name)
        if region is None:
            raise SystemExit(f"[錯誤] 找不到裁切預設「{name}」，請在 config 的 crop_presets 定義")
        pip = None
        if isinstance(region, dict):
            pip = region.get("pip")
            region = region["region"]
        rw, rh, rx, ry = clamp_crop(region, sw, sh)
        bg = lay.get("background_color", "0x202020")
        main = (f"crop={rw}:{rh}:{rx}:{ry},scale={CW}:{CH}:force_original_aspect_ratio=increase,"
                f"crop={CW}:{CH},setsar=1")
        if pip:
            pw, ph, px, py = clamp_crop(pip["region"], sw, sh)
            ax, ay, aw = pip["at"]
            f = (f"[0:v]split[m][p];[m]{main}[mc];"
                 f"[p]crop={pw}:{ph}:{px}:{py},scale={even(aw)}:-2,setsar=1[pc];"
                 f"[mc][pc]overlay={ax}:{ay},pad={W}:{H}:{X}:{Y}:color={bg}")
        else:
            f = f"[0:v]{main},pad={W}:{H}:{X}:{Y}:color={bg}"
        if has_template:
            return f + "[base];[1:v]scale=" + f"{W}:{H},format=rgba[tpl];[base][tpl]overlay=0:0:shortest=1[out]"
        return f + "[out]"
    if mode == "center_crop":
        return f"[0:v]crop=ih*9/16:ih,scale={W}:{H},setsar=1[out]"
    if mode == "blur_pad":
        return (f"[0:v]split[a][b];"
                f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,setsar=1[bg];"
                f"[b]scale={W}:-2,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[out]")
    # stack: game on top, avatar (contained, over blurred game) below
    gw, gh, gx, gy = clamp_crop(lay["game_crop"], sw, sh)
    aw, ah, ax, ay = clamp_crop(lay["avatar_crop"], sw, sh)
    ht = even(H * float(lay.get("game_height_ratio", 0.58)))
    hb = H - ht
    return (f"[0:v]split=3[a][b][c];"
            f"[a]crop={gw}:{gh}:{gx}:{gy},scale={W}:{ht}:force_original_aspect_ratio=increase,crop={W}:{ht},setsar=1[g];"
            f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{hb}:force_original_aspect_ratio=increase,crop={W}:{hb},boxblur=20:2,setsar=1[bg];"
            f"[c]crop={aw}:{ah}:{ax}:{ay},scale={W}:{hb}:force_original_aspect_ratio=decrease,setsar=1[av];"
            f"[bg][av]overlay=(W-w)/2:(H-h)/2[bot];"
            f"[g][bot]vstack[out]")


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s or "").strip("_")
    return s[:40]


# ---------------- subtitles ----------------

def phrases_in(segs, a, b, pause):
    """Word runs inside [a, b]: one per transcript segment, also split at pauses longer than `pause`.
    Whisper's segment boundaries are the most reliable sentence breaks we have (large-v3 output
    often has no punctuation at all), so cues never span two segments."""
    out = []
    for s in segs:
        if s["end"] < a or s["start"] > b:
            continue
        ws = s.get("words") or []
        if not ws:  # spread segment text evenly if no word timing
            chars = list(s["text"])
            d = (s["end"] - s["start"]) / max(1, len(chars))
            ws = [{"start": s["start"] + i * d, "end": s["start"] + (i + 1) * d, "word": ch}
                  for i, ch in enumerate(chars)]
        run = []
        for w in ws:
            if not a <= (w["start"] + w["end"]) / 2 <= b:
                continue
            if run and w["start"] - run[-1]["end"] > pause:
                out.append(run)
                run = []
            run.append(w)
        if run:
            out.append(run)
    return out


def clean(text):
    text = text.strip()
    text = re.sub(f"[{re.escape(PUNCT_SOFT)}]+$", "", text)
    text = re.sub(f"[{re.escape(PUNCT_SOFT)}]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def text_of(words):
    return clean("".join(w["word"] for w in words))


def word_boundaries(words):
    """Indices i where a line may break before words[i] without splitting a Chinese word.
    Uses jieba when installed (optional); returns None otherwise."""
    try:
        import jieba
    except ImportError:
        return None
    jieba.setLogLevel(60)
    cuts, pos = set(), 0
    for tok in jieba.cut("".join(w["word"] for w in words)):
        pos += len(tok)
        cuts.add(pos)
    ok, pos = set(), 0
    for i, w in enumerate(words):
        if i and pos in cuts:
            ok.add(i)
        pos += len(w["word"])
    return ok


def split_phrase(words, max_chars):
    """Split one phrase into lines of <= max_chars, roughly balanced (15 chars -> ~8 + 7).
    Chinese has no spaces, so among the allowed break points pick the one with the longest
    pause between words (speakers breathe at word/phrase boundaries); spaces and punctuation
    count as a long pause. With jieba installed, breaks inside a word are not allowed."""
    n_chars = len(text_of(words))
    if n_chars <= max_chars or len(words) < 2:
        return [words]
    n = math.ceil(n_chars / max_chars)
    target = n_chars / n
    allowed = word_boundaries(words)
    best, best_score = None, None
    for i in range(1, len(words)):
        left = len(text_of(words[:i]))
        if left < target * 0.6 or left > max_chars or n_chars - left < 2:
            continue
        if allowed is not None and i not in allowed:
            continue
        gap = words[i]["start"] - words[i - 1]["end"]
        prev_w = words[i - 1]["word"]
        if prev_w.rstrip() != prev_w or words[i]["word"].startswith(" ") or \
                (prev_w.strip() and prev_w.strip()[-1] in PUNCT_END + PUNCT_SOFT):
            gap += 1.0
        score = gap - 0.02 * abs(left - target)  # prefer pauses, then balance
        if best_score is None or score > best_score:
            best, best_score = i, score
    if best is None:  # no allowed break point: cut at the size limit
        best = next((i for i in range(1, len(words)) if len(text_of(words[:i + 1])) > max_chars), len(words) - 1)
    return [words[:best]] + split_phrase(words[best:], max_chars)


def build_cues(tsegs, csegs, max_chars, pause):
    cues, offset = [], 0.0
    for cs in csegs:
        a, b = cs["a"], cs["b"]
        lines = []
        for ph in phrases_in(tsegs, a, b, pause):
            lines += split_phrase(ph, max_chars)
        merged = []
        for ln in lines:
            # glue very short neighbouring phrases ("再來" "再來") so one-word cues don't flash by
            if merged and ln and merged[-1] and ln[0]["start"] - merged[-1][-1]["end"] <= 0.3 and \
                    (len(text_of(merged[-1])) < 4 or len(text_of(ln)) < 3) and \
                    len(text_of(merged[-1])) + 1 + len(text_of(ln)) <= max_chars:
                merged[-1] = merged[-1] + [{"word": " ", "start": ln[0]["start"], "end": ln[0]["start"]}] + ln
            else:
                merged.append(ln)
        for ln in merged:
            txt = text_of(ln)
            if txt:
                st = ln[0]["start"] - a + offset
                en = ln[-1]["end"] - a + offset
                cues.append([max(0.0, st), max(st + 0.5, en), txt])
        offset += b - a
    for i in range(len(cues) - 1):
        if cues[i][1] > cues[i + 1][0]:
            cues[i][1] = cues[i + 1][0]
    return cues


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8-sig") as f:  # BOM helps Premiere on Windows
        for i, (st, en, txt) in enumerate(cues, 1):
            f.write(f"{i}\n{srt_time(st)} --> {srt_time(en)}\n{txt}\n\n")


# ---------------- render ----------------

def render(src, info, clip, cfg, out_mp4, preview=False):
    enc = cfg["encode"]
    lay = dict(cfg["vertical_layout"])
    lay.update(clip.get("layout", {}))
    vertical = clip["format"] == "vertical"
    tpl = resolve_template(lay) if vertical and lay.get("mode", "template") == "template" else None

    def seg_cmd(seg, start, extra):
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src)]
        if tpl:
            cmd += ["-loop", "1", "-i", str(tpl)]
        if vertical:
            fl = vertical_filter(lay, info["width"], info["height"], seg.get("crop"), bool(tpl))
            cmd += ["-filter_complex", fl, "-map", "[out]"]
        else:
            cmd += ["-map", "0:v:0"]
        return cmd + extra

    if preview:
        outs = []
        for i, seg in enumerate(clip["_segs"]):
            jpg = out_mp4.with_name(out_mp4.stem + f"_s{i + 1}.jpg")
            mid = (seg["a"] + seg["b"]) / 2
            run(seg_cmd(seg, mid, ["-frames:v", "1", "-q:v", "3", str(jpg)]))
            outs.append(jpg)
        return outs

    tmp = Path(tempfile.mkdtemp(prefix="vh_"))
    parts = []
    try:
        for i, seg in enumerate(clip["_segs"]):
            part = tmp / f"p{i:03d}.mp4"
            extra = ["-t", f"{seg['b'] - seg['a']:.3f}", "-map", "0:a:0?",
                     "-c:v", "libx264", "-preset", enc["preset"], "-crf", str(enc["crf"]),
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", enc["audio_bitrate"],
                     "-ar", "48000", "-ac", "2", str(part)]
            run(seg_cmd(seg, seg["a"], extra))
            parts.append(part)
        joined = tmp / "joined.mp4"
        if len(parts) == 1:
            shutil.move(str(parts[0]), joined)
        else:
            lst = tmp / "list.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                 "-c", "copy", str(joined)])
        lufs = enc.get("loudness_lufs")
        if lufs is not None:
            # whole-clip loudness normalisation; video is copied untouched
            run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-c:v", "copy",
                 "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11", "-c:a", "aac",
                 "-b:a", enc["audio_bitrate"], "-ar", "48000", "-movflags", "+faststart", str(out_mp4)])
        else:
            shutil.move(str(joined), out_mp4)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--srt-only", action="store_true", help="只重產字幕（例如重轉之後），不重新輸出影片")
    args = ap.parse_args()
    need_tool("ffmpeg")
    cfg = load_config(args.config)
    paths = work_paths(args.workdir)
    sel = load_json(paths["selections"])
    src = Path(sel["source"])
    info = probe(src)
    tsegs = load_best_transcript(paths)  # refined windows (refine_transcript.py) win when present
    if paths["refined_json"].exists():
        print("字幕使用 transcript_refined.json（高品質重轉）")
    paths["clips"].mkdir(exist_ok=True)
    sub = cfg["subtitles"]

    manifest = []
    for clip in sel["clips"]:
        if args.only and clip["id"] not in args.only:
            continue
        clip["_segs"] = normalize_segments(clip)
        for s in clip["_segs"]:
            if not (0 <= s["a"] < s["b"] <= info["duration"] + 0.5):
                raise SystemExit(f"[錯誤] {clip['id']} 的區段 {s['a']}-{s['b']} 超出影片範圍")
        length = sum(s["b"] - s["a"] for s in clip["_segs"])
        base = paths["clips"] / f"{clip['id']}_{safe_name(clip.get('title'))}"
        out = base.with_suffix(".mp4")
        print(f"{clip['id']} [{clip['format']}] {length:.1f}s ← " +
              ", ".join(f"{fmt_ts(s['a'])}-{fmt_ts(s['b'])}" + (f"({s['crop']})" if s.get("crop") else "")
                        for s in clip["_segs"]))
        if args.srt_only:
            if not out.exists():
                raise SystemExit(f"[錯誤] 找不到 {out.name}，請先不加 --srt-only 輸出一次")
            res = out
        else:
            res = render(src, info, clip, cfg, out, preview=args.preview)
        if args.preview:
            for r in res:
                print(f"   → {r}")
            continue
        notes = []
        for n in clip.get("notes", []):
            t = source_to_clip_time(clip["_segs"], parse_ts(n["at"]))
            if t is not None:
                notes.append({"t": round(t, 3), "text": n["text"]})
        entry = {"id": clip["id"], "format": clip["format"], "title": clip.get("title", ""),
                 "header_title": clip.get("header_title", ""),
                 "length_sec": round(length, 2), "file": str(res),
                 "segments": [[s["a"], s["b"]] for s in clip["_segs"]], "notes": notes}
        mx = sub["vertical_max_chars"] if clip["format"] == "vertical" else sub["horizontal_max_chars"]
        cues = build_cues(tsegs, clip["_segs"], mx, sub["pause_split_sec"])
        if cues:
            write_srt(cues, base.with_suffix(".srt"))
            entry["srt"] = str(base.with_suffix(".srt"))
            entry["subtitle_lines"] = len(cues)
        manifest.append(entry)
        print(f"   → {res}")
    if not args.preview:
        # with --only, keep the other clips' entries so export_premiere_xml.py still sees every clip
        mpath = paths["clips"] / "manifest.json"
        done = {e["id"] for e in manifest}
        old = load_json(mpath)["clips"] if (args.only and mpath.exists()) else []
        order = [c["id"] for c in sel["clips"]]
        allc = [e for e in old if e["id"] not in done] + manifest
        allc.sort(key=lambda e: order.index(e["id"]) if e["id"] in order else len(order))
        save_json({"source": str(src), "clips": allc}, mpath)
    print(f"完成 {len(manifest) if not args.preview else '預覽'}")


if __name__ == "__main__":
    main()
