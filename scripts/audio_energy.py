"""Per-second loudness of the stream audio -> audio_energy.csv (t, rms_db).

Usage: python audio_energy.py --video STREAM.mp4 --workdir WORK
Streams decoded audio through ffmpeg so memory stays small even for 6h streams.
"""
import argparse
import subprocess

import numpy as np

from common import need_tool, work_paths

SR = 8000  # loudness only; low rate keeps it fast


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    need_tool("ffmpeg")
    paths = work_paths(args.workdir)

    cmd = ["ffmpeg", "-v", "error", "-i", args.video, "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    chunk = SR * 2  # bytes per second (int16)
    rows, t, buf = [], 0, b""
    while True:
        data = proc.stdout.read(chunk * 60)
        if not data:
            break
        buf += data
        while len(buf) >= chunk:
            sec, buf = buf[:chunk], buf[chunk:]
            x = np.frombuffer(sec, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
            rows.append((t, 20 * np.log10(rms)))
            t += 1
    proc.wait()
    with open(paths["audio"], "w", encoding="utf-8") as f:
        f.write("t,rms_db\n")
        for t, db in rows:
            f.write(f"{t},{db:.2f}\n")
    print(f"完成：{len(rows)} 秒的音量資料 → {paths['audio']}")


if __name__ == "__main__":
    main()
