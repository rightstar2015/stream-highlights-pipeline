"""Per-second loudness of the stream audio -> audio_energy.csv (t, rms_db).

Usage: python audio_energy.py --video STREAM.mp4 --workdir WORK [--start 0] [--end SECONDS]
Streams decoded audio through ffmpeg so memory stays small even for 6h streams.
"""
import argparse
import subprocess
import sys
import tempfile

import numpy as np

from common import atomic_write, need_tool, parse_ts, work_paths

SR = 8000  # loudness only; low rate keeps it fast


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--start", default=None, help="只分析某段，秒數或 HH:MM:SS")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    need_tool("ffmpeg")
    paths = work_paths(args.workdir)

    start = int(parse_ts(args.start)) if args.start else 0
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", args.video]
    if args.end:
        cmd += ["-t", f"{parse_ts(args.end) - start:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    # stderr goes to a file: a pipe left unread could fill up and stall ffmpeg
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        chunk = SR * 2  # bytes per second (int16)
        rows, t, buf = [], start, b""
        while True:
            data = proc.stdout.read(chunk * 60)
            if not data:
                break
            buf += data
            n = len(buf) // chunk  # whole seconds at once, so the buffer is copied once per read
            if n:
                x = np.frombuffer(buf[:n * chunk], dtype=np.int16).astype(np.float32).reshape(n, SR) / 32768.0
                rms = np.sqrt(np.mean(x * x, axis=1)) + 1e-9
                rows += zip(range(t, t + n), (20 * np.log10(rms)).tolist())
                t += n
                buf = buf[n * chunk:]
        rc = proc.wait()
        err.seek(0)
        tail = err.read().decode("utf-8", "replace").strip().splitlines()[-5:]
    if rc != 0 or not rows:
        # write nothing: an empty CSV would look finished to pipeline.py and be skipped forever
        sys.exit(f"[錯誤] 讀不到音訊（ffmpeg 回傳 {rc}）：" + " / ".join(tail))
    with atomic_write(paths["audio"]) as f:
        f.write("t,rms_db\n")
        for t, db in rows:
            f.write(f"{t},{db:.2f}\n")
    print(f"完成：{len(rows)} 秒的音量資料 → {paths['audio']}")


if __name__ == "__main__":
    main()
