"""Analyse finished highlight videos (the client's past deliverables) to learn their style.

Usage:
  python analyze_reference.py --out REFWORK video1.mp4 video2.mp4 ...

For each video writes to REFWORK/:
  <name>_sheet.jpg   timestamped contact sheet (read it to see layout / subtitles / effects)
  report.md          one row per video: format, length, cut rhythm, loudness,
                     and for vertical videos the moving "content window" (y range)
Large files (>400 MB) are fine here: this runs locally. Proxies (720p) work equally well.
"""
import argparse
import subprocess
from pathlib import Path

import numpy as np

from common import fmt_ts, need_tool, probe, run


def cuts(path):
    res = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-an", "-vf",
                          "scale=320:-2,select='gt(scene,0.12)',showinfo", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = []
    for line in res.stderr.splitlines():
        if "pts_time:" in line:
            out.append(float(line.split("pts_time:")[1].split()[0]))
    return out


def loudness(path):
    res = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af", "ebur128",
                          "-f", "null", "-"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    lines = res.stderr.splitlines()
    for i, line in enumerate(lines):
        if "Summary:" in line:
            for l2 in lines[i:i + 8]:
                if l2.strip().startswith("I:"):
                    return float(l2.split()[1])
    return None


def content_window(path, info):
    """Rows that change over time = the moving content area inside a fixed template."""
    w, h = info["width"], info["height"]
    sw, sh = 270, int(270 * h / w)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          f"fps=0.5,scale={sw}:{sh},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8)
    n = len(fr) // (sw * sh)
    if n < 3:
        return None
    fr = fr[: n * sw * sh].reshape(n, sh, sw).astype(np.float32)
    dyn = fr.std(axis=0).mean(axis=1) > 6
    best, s = (0, 0), None
    for y in range(sh + 1):
        on = y < sh and dyn[y]
        if on and s is None:
            s = y
        if not on and s is not None:
            if y - s > best[1] - best[0]:
                best = (s, y)
            s = None
    k = h / sh
    return int(best[0] * k), int(best[1] * k)


def sheet(path, info, out):
    dur = info["duration"]
    vertical = info["height"] > info["width"]
    cols, rows = (6, 4) if vertical else (5, 8)
    step = max(1.0, dur / (cols * rows))
    size = "216:384" if vertical else "320:180"
    ts = "drawtext=text='%{pts\\:hms}':x=6:y=6:fontsize=18:fontcolor=yellow:box=1:boxcolor=black@0.6"
    # Windows ffmpeg builds often lack a fontconfig config, so point drawtext at a system font directly
    font = Path("C:/Windows/Fonts/arial.ttf")
    if font.exists():
        ts += ":fontfile='" + font.as_posix().replace(":", "\\:") + "'"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf",
         f"fps=1/{step:.3f},scale={size},{ts},tile={cols}x{rows}", "-frames:v", "1", "-q:v", "3", str(out)])
    return step


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("videos", nargs="+")
    args = ap.parse_args()
    need_tool("ffmpeg")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = ["| 檔案 | 格式 | 長度 | 剪接/換畫面數 | 平均間隔 | 中位間隔 | 響度 LUFS | 內容視窗 y | 縮圖間隔 |",
            "|---|---|---|---|---|---|---|---|---|"]
    for v in args.videos:
        p = Path(v)
        info = probe(p)
        vertical = info["height"] > info["width"]
        c = cuts(p)
        gaps = np.diff([0.0] + c + [info["duration"]]) if c else np.array([info["duration"]])
        lu = loudness(p)
        win = content_window(p, info) if vertical else None
        step = sheet(p, info, out / f"{p.stem}_sheet.jpg")
        rows.append(f"| {p.name} | {'直式' if vertical else '橫式'} {info['width']}x{info['height']} "
                    f"{info['fps']:.0f}fps | {fmt_ts(info['duration'])} ({info['duration']:.1f}s) | {len(c)} | "
                    f"{gaps.mean():.2f}s | {np.median(gaps):.2f}s | {lu if lu is not None else '-'} | "
                    f"{f'{win[0]}–{win[1]}' if win else '-'} | 每 {step:.1f}s |")
        print(f"完成 {p.name}")
    (out / "report.md").write_text(
        "# 參考成品分析\n\n" + "\n".join(rows) +
        "\n\n換畫面數包含放大、插入、轉場（scene 閾值 0.12），不只是剪接點。\n"
        "接著讀每支的 *_sheet.jpg，把觀察到的版型、字幕、特效手法補進 references/client_style.md。\n",
        encoding="utf-8")
    print(f"報告 → {out / 'report.md'}")


if __name__ == "__main__":
    main()
