"""Optional: guess who said each subtitle line in a multi-person stream, with pyannote.

Usage:
  python diarize.py --video STREAM.mp4 --workdir WORK [--num-speakers 3]  -> WORK/speakers.json (about 2 min per hour on GPU)
  python diarize.py --workdir WORK --label                 -> clips/<id>_<title>_說話者.srt
  then cut_clips.py (or cut_clips.py --srt-only) splits subtitles per speaker: clips/<id>_<title>_<name>.srt

Knowing how many people talk (config "speakers": {"count": 3} or --num-speakers) stops the model
from splitting one voice into two clusters.

Needs pyannote.audio and a Hugging Face login with the model's terms accepted (see README); run it
with the Python that has them. Measured at about 76% on one 3-person stream, so labels are a draft
for sorting subtitle cards onto speaker tracks, not an answer. Original SRT files are left untouched.
Lines nobody clearly owns get "?". Name the clusters in WORK/speaker_map.json
({"SPEAKER_00": "小明", "SPEAKER_02": "merge:SPEAKER_00", "SPEAKER_03": "ignore"}) or in config
"speakers": {"map": {...}}; after a run, the longest solo turn of each cluster is printed to listen to.
"""
import argparse
from bisect import bisect_left
import re
import subprocess
import sys

from pathlib import Path

import speakers as spkmod
from common import fmt_ts, load_config, load_json, need_tool, normalize_segments, save_json, work_paths

MODEL = "pyannote/speaker-diarization-community-1"
SR = 16000
MIN_SHARE = 0.5  # the top speaker must cover this much of a line's spoken time
CUE_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def run_model(video, num_speakers=None):
    import numpy as np
    import torch
    from pyannote.audio import Pipeline
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    wave = torch.from_numpy(np.frombuffer(raw, dtype=np.float32).copy()).unsqueeze(0)
    pipe = Pipeline.from_pretrained(MODEL, token=True)
    if torch.cuda.is_available():
        pipe.to(torch.device("cuda"))
    result = pipe({"waveform": wave, "sample_rate": SR}, **({"num_speakers": num_speakers} if num_speakers else {}))
    ann = getattr(result, "speaker_diarization", result)
    return [[round(t.start, 2), round(t.end, 2), spk] for t, _, spk in ann.itertracks(yield_label=True)]


def speaker_for(a, b, turns):
    """Who talks most in source range a-b -> label, or None when nobody clearly owns it."""
    talk = {}
    for start, end, spk in turns:
        amount = min(b, end) - max(a, start)
        if amount > 0:
            talk[spk] = talk.get(spk, 0) + amount
    if not talk:
        return None
    top = max(talk, key=talk.get)
    return top if talk[top] / sum(talk.values()) >= MIN_SHARE else None


def summary(turns):
    """Per cluster: total talk time and its longest stretch with nobody else talking (to listen to)."""
    out = {}
    turns = sorted(turns, key=lambda t: t[0])
    starts = [t[0] for t in turns]
    longest = max((b - a for a, b, _ in turns), default=0)
    for a, b, s in turns:
        d = out.setdefault(s, {"total": 0.0, "solo": None})
        d["total"] += b - a
        near = turns[bisect_left(starts, a - longest):bisect_left(starts, b)]
        clash = any(o != s and oa < b and ob > a for oa, ob, o in near)
        if not clash and (d["solo"] is None or b - a > d["solo"][1] - d["solo"][0]):
            d["solo"] = (a, b)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["total"]))


def clip_to_source(segs, t):
    """Inverse of common.source_to_clip_time: a time in the clip -> a time in the stream."""
    for s in segs:
        length = s["b"] - s["a"]
        if t <= length:
            return s["a"] + t
        t -= length
    return segs[-1]["b"]


def label_srt(text, segs, turns, names=None):
    """Prefix every cue with its speaker. Timing and text stay as they are."""
    names = names or {}
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        out.append(line)
        m = CUE_TIME.match(line.strip())
        if m and i + 1 < len(lines) and lines[i + 1].strip():
            v = list(map(int, m.groups()))
            a = v[0] * 3600 + v[1] * 60 + v[2] + v[3] / 1000
            b = v[4] * 3600 + v[5] * 60 + v[6] + v[7] / 1000
            spk = speaker_for(clip_to_source(segs, a), clip_to_source(segs, b), turns)
            lines[i + 1] = f"[{names.get(spk, spk) if spk else '?'}] {lines[i + 1]}"
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--label", action="store_true", help="依 speakers.json 產生帶說話者的 SRT")
    ap.add_argument("--num-speakers", type=int, help="說話人數（優先於 config 的 speakers.count）")
    args = ap.parse_args()
    paths = work_paths(args.workdir)
    store = paths["dir"] / "speakers.json"

    cfg = load_config(args.config)
    if args.video:
        need_tool("ffmpeg")
        count = args.num_speakers or (cfg.get("speakers") or {}).get("count")
        if count is not None and (not isinstance(count, int) or count < 1):
            sys.exit("[錯誤] 說話人數要是正整數")
        try:
            turns = run_model(args.video, count)
        except ImportError:
            sys.exit("[錯誤] 沒有 pyannote.audio，安裝方式見 README 的「說話者辨識」")
        video = Path(args.video)
        save_json({"model": MODEL, "num_speakers": count,
                   "source": {"name": video.name, "size": video.stat().st_size}, "turns": turns}, store)
        stats = summary(turns)
        print(f"找到 {len(stats)} 位說話者、{len(turns)} 段 → {store}" + (f"（指定 {count} 人）" if count else ""))
        for s, d in stats.items():
            solo = f"，最長的獨白 {fmt_ts(d['solo'][0])}–{fmt_ts(d['solo'][1])}" if d["solo"] else ""
            print(f"  {s}：共 {d['total'] / 60:.1f} 分鐘{solo}")
        print("聽過之後把名字寫進 WORK/speaker_map.json，例如 {\"SPEAKER_00\": \"名字\"}，"
              "不是人聲的群寫 \"ignore\"，同一人被拆成兩群寫 \"merge:SPEAKER_00\"")
    if not args.label:
        if not args.video:
            sys.exit("[錯誤] 要給 --video 或 --label")
        return
    if not store.exists():
        sys.exit("[錯誤] 還沒有 speakers.json，先用 --video 跑一次")

    turns = load_json(store)["turns"]
    work_map = paths["dir"] / "speaker_map.json"
    who = spkmod.Speakers(turns, spkmod.name_map(cfg.get("speakers"), load_json(work_map) if work_map.exists() else None))
    names = {s: who.resolve(s) for _, _, s in turns}
    done = 0
    for c in load_json(paths["clips"] / "manifest.json")["clips"]:
        src = Path(c["srt"]) if c.get("srt") else None
        clip = next((x for x in load_json(paths["selections"])["clips"] if x["id"] == c["id"]), None)
        if src is None or not src.exists() or clip is None:
            continue
        text = src.read_text(encoding="utf-8-sig")
        out = src.with_name(src.stem + "_說話者.srt")
        out.write_text(label_srt(text, normalize_segments(clip), turns, names), encoding="utf-8-sig")
        print(f"→ {out}")
        done += 1
    print(f"完成 {done} 支。標籤只是草稿（單場實測約 76%），請對照畫面確認。")


if __name__ == "__main__":
    main()
