"""Per-second voice reaction signals (screams, laughter) -> vocal_events.csv.

Usage: python vocal_events.py --workdir WORK [--video STREAM.mp4] [--start 0] [--end SECONDS]

Prefers WORK/vocals16k.wav from `transcribe.py --separate` (game music removed), so the
loudness and pitch measured here are the streamer's voice; falls back to the stream audio.
Heuristic, no model needed. Columns:
  vocal_rel_db  voice loudness above the local 5-minute baseline
  scream        loud bursts with most energy in 1-4 kHz (shrieks, yelling)
  laugh         loudness pulsing at 3-7 Hz, the rhythm of "哈哈哈"
"""
import argparse
import subprocess
import time

import numpy as np

from common import atomic_write, fmt_ts, load_json, need_tool, parse_ts, work_paths

SR = 16000
SUB = 320            # 20 ms sub-frames -> 50 per second
PER_SEC = SR // SUB
FREQS = np.fft.rfftfreq(SUB, 1 / SR)
LOW = (FREQS >= 80) & (FREQS < 4000)
HIGH = (FREQS >= 1000) & (FREQS < 4000)


def decode(src, start, dur):
    """Yield 1-second float32 chunks of 16 kHz mono audio."""
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    chunk = SR * 2
    while True:
        buf = proc.stdout.read(chunk)
        if len(buf) < chunk:
            break
        yield np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
    proc.wait()


def frame_features(x):
    """Per 20 ms sub-frame: level in dB and share of voice-band energy above 1 kHz."""
    fr = x[: PER_SEC * SUB].reshape(PER_SEC, SUB)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-9)
    spec = np.abs(np.fft.rfft(fr * np.hanning(SUB), axis=1)) ** 2
    hf = spec[:, HIGH].sum(axis=1) / (spec[:, LOW].sum(axis=1) + 1e-12)
    return db, hf


def rolling_median(x, w):
    """Local baseline: median over blocks, then interpolate (same idea as signals.py)."""
    n = len(x)
    centers, vals = [], []
    for s in range(0, n, max(1, w // 4)):
        a, b = max(0, s - w // 2), min(n, s + w // 2)
        centers.append(s)
        vals.append(np.median(x[a:b]))
    return np.interp(np.arange(n), centers, vals)


def laugh_score(env, active):
    """Share of envelope modulation at 3-7 Hz, scaled by depth and voice activity.
    Steps (sudden loudness changes) have mostly <3 Hz energy, so they score low."""
    if active < 0.3 or env.mean() < 1e-6:
        return 0.0
    e = (env - env.mean()) * np.hanning(len(env))
    p = np.abs(np.fft.rfft(e)) ** 2
    f = np.fft.rfftfreq(len(env), 1 / PER_SEC)
    band = p[(f >= 3) & (f <= 7)].sum()
    total = p[(f >= 0.5) & (f <= 12)].sum() + 1e-12
    depth = np.sqrt(band / len(env)) / env.mean()
    return float(band / total * min(1.0, depth * 2) * active)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--video", help="沒有 vocals16k.wav 時改用直播檔的音軌")
    ap.add_argument("--start", default=None, help="只分析某段（直播檔時間）")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    need_tool("ffmpeg")
    paths = work_paths(args.workdir)
    start = parse_ts(args.start) if args.start else 0.0
    end = parse_ts(args.end) if args.end else None

    if paths["vocals_wav"].exists():
        src = paths["vocals_wav"]
        # the vocals file starts where transcribe.py --start began
        off = 0.0
        if paths["transcript_json"].exists():
            off = float(load_json(paths["transcript_json"]).get("vocals_offset", 0.0))
        print("使用分離後的人聲（vocals16k.wav）")
    elif args.video:
        src, off = args.video, 0.0
        print("[提示] 沒有 vocals16k.wav，改用直播音軌（遊戲配樂會干擾；建議轉錄時加 --separate）")
    else:
        raise SystemExit("[錯誤] 沒有 vocals16k.wav，請加 --video 直播檔")

    t0 = time.time()
    rel_start = max(0.0, start - off)
    db_sub, hf_sub = [], []
    for x in decode(src, rel_start, (end - start) if end else None):
        d, h = frame_features(x)
        db_sub.append(d)
        hf_sub.append(h)
    if not db_sub:
        raise SystemExit("[錯誤] 讀不到音訊")
    db_sub, hf_sub = np.array(db_sub), np.array(hf_sub)      # (seconds, 50)
    n = len(db_sub)
    sec_db = 10 * np.log10((10 ** (db_sub / 10)).mean(axis=1) + 1e-12)
    base = rolling_median(sec_db, 300)
    rel = np.clip(sec_db - base, 0, None)

    scream = np.zeros(n)
    laugh = np.zeros(n)
    env_all = 10 ** (db_sub / 20)                           # linear sub-frame envelope
    for t in range(n):
        over = np.clip(db_sub[t] - base[t] - 6, 0, None)
        scream[t] = float((over * hf_sub[t]).mean())
        a, b = max(0, t - 1), min(n, t + 1)                 # 2-second window centred on t
        env = env_all[a:b].ravel()
        active = float((db_sub[a:b] > base[t] + 3).mean())
        laugh[t] = laugh_score(env, active)

    first = int(round(rel_start + off))
    with atomic_write(paths["vocal"]) as f:
        f.write("t,vocal_rel_db,scream,laugh\n")
        for i in range(n):
            f.write(f"{first + i},{rel[i]:.1f},{scream[i]:.2f},{laugh[i]:.3f}\n")
    print(f"完成：{fmt_ts(first)}–{fmt_ts(first + n)}，耗時 {(time.time()-t0)/60:.1f} 分 → {paths['vocal']}")


if __name__ == "__main__":
    main()
