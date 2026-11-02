"""Optional: guess who said each subtitle line in a multi-person stream, with pyannote.

Usage:
  python diarize.py --video STREAM.mp4 --workdir WORK      -> WORK/speakers.json (about 2 min per hour on GPU)
  python diarize.py --workdir WORK --label                 -> clips/<id>_<title>_說話者.srt

Needs pyannote.audio and a Hugging Face login with the model's terms accepted (see README); run it
with the Python that has them. Measured at about 76% on one 3-person stream, so labels are a draft
for sorting subtitle cards onto speaker tracks, not an answer. Original SRT files are left untouched.
Lines nobody clearly owns get "?". Name the speakers in config: "speakers": {"SPEAKER_01": "小明"}.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

from common import load_config, load_json, need_tool, normalize_segments, save_json, work_paths

MODEL = "pyannote/speaker-diarization-community-1"
SR = 16000
MIN_SHARE = 0.5  # the top speaker must cover this much of a line's spoken time
CUE_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def run_model(video):
    import numpy as np
    import torch
    from pyannote.audio import Pipeline
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    wave = torch.from_numpy(np.frombuffer(raw, dtype=np.float32).copy()).unsqueeze(0)
    pipe = Pipeline.from_pretrained(MODEL, token=True)
    if torch.cuda.is_available():
        pipe.to(torch.device("cuda"))
    result = pipe({"waveform": wave, "sample_rate": SR})
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
    args = ap.parse_args()
    paths = work_paths(args.workdir)
    store = paths["dir"] / "speakers.json"

    if args.video:
        need_tool("ffmpeg")
        try:
            turns = run_model(args.video)
        except ImportError:
            sys.exit("[錯誤] 沒有 pyannote.audio，安裝方式見 README 的「說話者辨識」")
        save_json({"model": MODEL, "turns": turns}, store)
        print(f"找到 {len({t[2] for t in turns})} 位說話者、{len(turns)} 段 → {store}")
    if not args.label:
        if not args.video:
            sys.exit("[錯誤] 要給 --video 或 --label")
        return
    if not store.exists():
        sys.exit("[錯誤] 還沒有 speakers.json，先用 --video 跑一次")

    turns = load_json(store)["turns"]
    names = load_config(args.config).get("speakers", {})
    done = 0
    for c in load_json(paths["clips"] / "manifest.json")["clips"]:
        src = next((p for p in paths["clips"].glob(f"{c['id']}_*.srt") if not p.stem.endswith("_說話者")), None)
        clip = next((x for x in load_json(paths["selections"])["clips"] if x["id"] == c["id"]), None)
        if src is None or clip is None:
            continue
        text = src.read_text(encoding="utf-8-sig")
        out = src.with_name(src.stem + "_說話者.srt")
        out.write_text(label_srt(text, normalize_segments(clip), turns, names), encoding="utf-8-sig")
        print(f"→ {out}")
        done += 1
    print(f"完成 {done} 支。標籤只是草稿（單場實測約 76%），請對照畫面確認。")


if __name__ == "__main__":
    main()
