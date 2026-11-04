"""Per-second on-screen change signals from the game area -> visual_events.csv.

Usage: python visual_events.py --video STREAM.mp4 --workdir WORK [--config config.json]
                               [--start 0] [--end SECONDS] [--fps 2]

Game-agnostic: no templates, just how much and how suddenly the picture changes.
Only the game region (vertical_layout.crop_presets.game) is analysed so the Live2D model's
constant motion doesn't count. Columns:
  motion  mean absolute difference between consecutive frames (0-255)
  cut     histogram change between consecutive frames (0-1, scene cut / menu / death screen)
  luma    mean brightness (0-255)
  flash   brightness jump above the previous few seconds (explosions, hit flashes, white-outs)
  fade    brightness drop below the previous few seconds (fade to black, death screens)
"""
import argparse
import subprocess
import time

import numpy as np

from common import atomic_write, fmt_ts, load_config, need_tool, parse_ts, probe, work_paths

W, H = 160, 90  # analysis size; enough to see big changes, keeps 6h streams fast
BINS = 16


def game_region(cfg, info):
    """Game crop from config, clamped to the source frame; whole frame if not usable."""
    reg = cfg["vertical_layout"].get("crop_presets", {}).get("game")
    if isinstance(reg, dict):
        reg = reg.get("region")
    sw, sh = info.get("width", 1920), info.get("height", 1080)
    if not reg or len(reg) != 4:
        return [0, 0, sw, sh]
    x, y, w, h = map(int, reg)
    x, y = max(0, min(x, sw - 16)), max(0, min(y, sh - 16))
    return [x, y, max(16, min(w, sw - x)), max(16, min(h, sh - y))]


def read_frames(video, region, fps, start, end):
    """Yield (time, gray uint8 HxW) frames from ffmpeg."""
    x, y, w, h = region
    cmd = ["ffmpeg", "-v", "error", "-hwaccel", "auto"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if end:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-an", "-vf", f"fps={fps},crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=area,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = W * H
    i = 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield start + i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(H, W)
        i += 1
    proc.wait()


def per_second(frames):
    """Aggregate frames into per-second motion / cut / luma."""
    rows = {}
    prev, prev_hist = None, None
    for t, f in frames:
        g = f.astype(np.float32)
        hist = np.histogram(f, bins=BINS, range=(0, 256))[0] / f.size
        motion = float(np.abs(g - prev).mean()) if prev is not None else 0.0
        cut = float(np.abs(hist - prev_hist).sum() / 2) if prev_hist is not None else 0.0
        s = int(t)
        r = rows.setdefault(s, {"motion": 0.0, "cut": 0.0, "luma": []})
        r["motion"] = max(r["motion"], motion)
        r["cut"] = max(r["cut"], cut)
        r["luma"].append(float(g.mean()))
        prev, prev_hist = g, hist
    return rows


def brightness_changes(luma, lookback=4):
    """flash = brighter than both the seconds before and after (a short spike, so coming back
    from a black screen doesn't count); fade = darker than the preceding seconds."""
    n = len(luma)
    flash, fade = np.zeros(n), np.zeros(n)
    for t in range(1, n):
        before = np.median(luma[max(0, t - lookback):t])
        after = np.median(luma[t + 1:t + 1 + lookback]) if t + 1 < n else before
        flash[t] = max(0.0, luma[t] - max(before, after))
        fade[t] = max(0.0, before - luma[t])
    return flash, fade


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--fps", type=float, default=2.0)
    args = ap.parse_args()
    need_tool("ffmpeg")
    cfg = load_config(args.config)
    paths = work_paths(args.workdir)
    info = probe(args.video)
    region = game_region(cfg, info)
    start = parse_ts(args.start) if args.start else 0.0
    end = parse_ts(args.end) if args.end else None

    t0 = time.time()
    print(f"分析畫面變化（遊戲區 {region}，每秒 {args.fps:g} 張）...")
    rows = per_second(read_frames(args.video, region, args.fps, start, end))
    if not rows:
        raise SystemExit("[錯誤] 讀不到任何畫面")
    secs = sorted(rows)
    luma = np.array([np.mean(rows[s]["luma"]) for s in secs])
    flash, fade = brightness_changes(luma)
    with atomic_write(paths["visual"]) as f:
        f.write("t,motion,cut,luma,flash,fade\n")
        for i, s in enumerate(secs):
            r = rows[s]
            f.write(f"{s},{r['motion']:.2f},{r['cut']:.3f},{luma[i]:.1f},{flash[i]:.1f},{fade[i]:.1f}\n")
    print(f"完成：{fmt_ts(secs[0])}–{fmt_ts(secs[-1])}，耗時 {(time.time()-t0)/60:.1f} 分 → {paths['visual']}")


if __name__ == "__main__":
    main()
