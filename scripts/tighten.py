"""Cut long pauses inside the chosen segments, turning each one into tighter jump cuts.

Usage: python tighten.py --workdir WORK [--config config.json] [--file selections.json] [--only v01 h02]
                         [--write]

A pause is a gap between two transcribed words longer than tighten.min_gap_sec. It is removed,
keeping tighten.pad_sec of air on both sides, unless something loud happens in it: audio louder than
the clip's speech by tighten.keep_loud_db (laughing or screaming is rarely transcribed).

Loudness is read every 50 ms, from WORK/vocals16k.wav when transcribe.py --separate made one (game
music removed), else from the stream. Whisper's word times often stop before a drawn-out ending or
a breath, so a pause only starts once the sound falls to the background level (within
tighten.quiet_db, or 30 dB under the speech), and ends where the sound rises again. A pause is kept
only when it is loud for at least 0.2 s, so a click or a single breath doesn't save it. Without any audio, per-second
audio_energy.csv is used instead, and drawn-out endings may be cut.

Silence before the first and after the last word of a segment counts too. Segments without words
stay as they are. Notes that fall in a removed pause move to the start of the next piece, so their
markers survive.

Without --write it only prints what would change. --write saves the file and keeps the previous
version next to it as <name>.before_tighten.json.
"""
import argparse
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np

from common import (fmt_ts, load_best_transcript, load_config, load_json, normalize_segments, parse_ts, save_json,
                    work_paths)

STEP = 0.05  # seconds per loudness frame
LOUD_FRAMES = 4  # a laugh or scream lasts; a click or a single breath doesn't (4 frames = 0.2 s)
QUIET_BELOW_SPEECH = 30  # dB under the speech level always counts as quiet, even over a near-silent background
RATE = 16000


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


def audio_reader(paths, source=None):
    """-> read(a, b) giving (start time, dB per 50 ms frame) for stream time [a, b], or None when
    there's no audio file to read."""
    if paths["vocals_wav"].exists():
        src = paths["vocals_wav"]
        tj = paths["transcript_json"]
        shift = float(load_json(tj).get("vocals_offset", 0.0)) if tj.exists() else 0.0  # wav starts at --start
    elif source and Path(source).is_file():
        src, shift = Path(source), 0.0
    else:
        return None

    def read(a, b):
        start = max(0.0, a - shift)
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{b - a:.3f}", "-i", str(src),
                            "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"], capture_output=True)
        per = int(RATE * STEP)
        n = len(r.stdout) // 2 // per
        if r.returncode or not n:
            return None
        x = np.frombuffer(r.stdout[:n * per * 2], dtype=np.int16).astype(np.float32).reshape(n, per) / 32768.0
        return start + shift, 20 * np.log10(np.sqrt(np.mean(x * x, axis=1)) + 1e-9)
    return read


def words_in(tsegs, a, b):
    ws = [w for s in tsegs for w in (s.get("words") or []) if a <= (w["start"] + w["end"]) / 2 <= b]
    return sorted(ws, key=lambda w: w["start"])


def loud(energy, a, b, speech_db, margin):
    """Per-second fallback: True when any second touching (a, b) is louder than speech by margin dB."""
    secs = [energy[t] for t in range(math.floor(a), math.ceil(b)) if t in energy]
    return speech_db is not None and bool(secs) and max(secs) >= speech_db + margin


def speech_level(energy, words):
    secs = sorted({int(w["start"]) for w in words if int(w["start"]) in energy})
    if not secs:
        return None
    vals = sorted(energy[t] for t in secs)
    return vals[len(vals) // 2]


class Fine:
    """50 ms loudness of one segment: speech level, background level and frame lookups."""

    def __init__(self, t0, db, words):
        self.t0, self.db = t0, db
        idx = [i for w in words for i in range(self.at(w["start"]), self.at(w["end"]) + 1) if 0 <= i < len(db)]
        self.speech = float(np.median(db[idx])) if idx else None
        self.bg = float(np.percentile(db, 10))

    def at(self, t):
        return int(round((t - self.t0) / STEP))

    def time(self, i):
        return self.t0 + i * STEP

    def window(self, x, y):
        return range(max(0, self.at(x)), min(len(self.db), self.at(y)))

    def loud(self, x, y, margin):
        if self.speech is None:
            return False
        win = self.window(x, y)
        return int(np.sum(self.db[win.start:win.stop] >= self.speech + margin)) >= LOUD_FRAMES

    def quiet_span(self, x, y, quiet_db):
        """Where the sound in (x, y) has fallen to the background: (first, last) quiet time, or None.
        Two quiet frames in a row are needed, so a single dip inside a breath doesn't count."""
        limit = self.bg + quiet_db
        if self.speech is not None:
            limit = max(limit, self.speech - QUIET_BELOW_SPEECH)
        frames = list(self.window(x, y))
        quiet = [i for i in frames[:-1] if self.db[i] < limit and self.db[i + 1] < limit]
        if not quiet:
            return None
        return self.time(quiet[0]), self.time(quiet[-1] + 2)


def pauses(a, b, words, gap, pad, energy, margin, fine=None, quiet_db=6):
    """Spans of [a, b] to remove: gaps longer than `gap`, shrunk by `pad`, that aren't loud."""
    if not words:
        return []
    level = fine.speech if fine else speech_level(energy, words)
    edges = [a] + [x for w in words for x in (w["start"], w["end"])] + [b]
    out = []
    for i in range(0, len(edges), 2):  # (end of speech, start of next speech) pairs
        x, y = edges[i], edges[i + 1]
        if y - x <= gap:
            continue
        if fine:
            if fine.loud(x, y, margin):
                continue
            span = fine.quiet_span(x, y, quiet_db)
            if span is None:
                continue
            x, y = max(x, span[0]), min(y, span[1])  # keep drawn-out endings and breaths before speech
            if y - x <= gap:
                continue
        elif loud(energy, x, y, level, margin):
            continue
        cut_a = edges[i] if i == 0 else x + pad  # the segment's own in/out point needs no air
        cut_b = edges[i + 1] if i + 2 == len(edges) else y - pad
        if cut_b - cut_a > 0.05:
            out.append((cut_a, cut_b))
    return out


def tighten_clip(clip, tsegs, energy, cfg, read=None):
    """-> (segments, notes, removed seconds). Each piece keeps its segment's shape and crop.
    read: audio_reader() result for 50 ms loudness; without it, per-second `energy` is used."""
    t = cfg.get("tighten", {})
    gap, pad, margin = t.get("min_gap_sec", 0.8), t.get("pad_sec", 0.2), t.get("keep_loud_db", 3)
    quiet_db = t.get("quiet_db", 6)
    pieces, removed = [], 0.0
    for raw, s in zip(clip["segments"], normalize_segments(clip)):
        a, b = s["a"], s["b"]
        ws = words_in(tsegs, a, b)
        got = read(a, b) if read and ws else None
        fine = Fine(*got, ws) if got else None
        cuts = pauses(a, b, ws, gap, pad, energy, margin, fine, quiet_db)
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
    read = audio_reader(paths, sel.get("source"))
    if read is None:
        print("[提示] 找不到 vocals16k.wav 或直播檔，改用每秒音量判斷；拉長的字尾可能被剪掉")
    elif not paths["vocals_wav"].exists():
        print("[提示] 沒有 vocals16k.wav，用直播音軌判斷（遊戲配樂大時，拉長的字尾比較難分辨）")
    for c in sel["clips"]:
        if args.only and c["id"] not in args.only:
            continue
        before = sum(s["b"] - s["a"] for s in normalize_segments(c))
        segs, notes, removed = tighten_clip(c, tsegs, energy, cfg, read)
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
