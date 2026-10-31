"""Grab frames so Claude can see what happened on screen.

Usage:
  python frames.py --video STREAM.mp4 --workdir WORK --times 01:02:03 4500 ...
      -> frames/<time>.jpg : 3 frames (t-6s, t, t+4s) side by side, 1920 px wide
  python frames.py --video STREAM.mp4 --workdir WORK --candidates [--limit 20]
      -> same, for the top candidates in candidates.json
  python frames.py --video STREAM.mp4 --workdir WORK --calibrate 600
      -> frames/calibrate.jpg : one full-res frame with a 100px grid, for setting crops
"""
import argparse

from common import fmt_ts, load_json, need_tool, parse_ts, run, work_paths


def strip(video, t, out):
    ts = [max(0, t - 6), t, t + 4]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for x in ts:
        cmd += ["-ss", f"{x:.2f}", "-i", str(video)]
    fl = ";".join(f"[{i}:v]trim=end_frame=1,scale=640:-2,setpts=PTS-STARTPTS[v{i}]" for i in range(3))
    fl += ";[v0][v1][v2]hstack=inputs=3[out]"
    cmd += ["-filter_complex", fl, "-map", "[out]", "-frames:v", "1", "-q:v", "4", str(out)]
    run(cmd)


def calibrate(video, t, out):
    grid = ("drawgrid=w=100:h=100:t=1:c=yellow@0.5,"
            "drawgrid=w=500:h=500:t=3:c=red@0.8")
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(video),
         "-frames:v", "1", "-vf", grid, "-q:v", "3", str(out)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--times", nargs="*")
    ap.add_argument("--candidates", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--calibrate")
    args = ap.parse_args()
    need_tool("ffmpeg")
    paths = work_paths(args.workdir)
    paths["frames"].mkdir(exist_ok=True)

    if args.calibrate is not None:
        out = paths["frames"] / "calibrate.jpg"
        calibrate(args.video, parse_ts(args.calibrate), out)
        print(f"校正畫面（黃線每 100px、紅線每 500px）→ {out}")
        return

    times = [parse_ts(t) for t in (args.times or [])]
    if args.candidates:
        cands = load_json(paths["candidates"])["candidates"]
        cands = sorted(cands, key=lambda c: c["rank"])[: args.limit]
        # events usually precede the chat peak, so look a bit earlier
        times += [max(0, c["peak"] - 8) for c in cands]
    for t in times:
        out = paths["frames"] / f"{fmt_ts(t).replace(':', '-')}.jpg"
        strip(args.video, t, out)
        print(out)


if __name__ == "__main__":
    main()
