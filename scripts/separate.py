"""Vocal separation with Demucs, so Whisper hears the streamer instead of the game music.

Usage (standalone, mostly for testing):
  python separate.py --video STREAM.mp4 --out vocals.wav [--start 1:00:00 --end 1:05:00]

Library use: load_separator(cfg) once, then separator(src, start, dur) -> 16 kHz mono float32 numpy.
Needs torch (CUDA build recommended) + demucs. Audio is decoded through ffmpeg, so torchaudio's
file backends are never used.
"""
import argparse
import subprocess
import wave

import numpy as np

from common import fmt_ts, load_config, need_tool, parse_ts

SR_DEMUCS = 44100
SR_WHISPER = 16000
CHUNK_SEC = 600  # long streams are separated in chunks to bound memory


def read_audio(src, start, dur, sr, channels):
    """Decode [start, start+dur) of src to float32 numpy, shape (channels, n)."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-vn", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, channels).T.copy()


def load_separator(cfg):
    """Return separate(src, start, dur) -> mono 16 kHz vocals, or raise ImportError with a hint."""
    try:
        import torch
        import torchaudio.functional as AF
        from demucs.apply import apply_model
        from demucs.pretrained import get_model
    except ImportError as e:
        raise ImportError("人聲分離需要 torch 與 demucs，請執行 setup_check.py 看安裝方式") from e

    scfg = cfg.get("separation", {})
    device = scfg.get("device", "auto")
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("[提示] 沒偵測到 CUDA，人聲分離用 CPU 會很慢。")
    model = get_model(scfg.get("model", "htdemucs"))
    model.to(device).eval()
    vocals_idx = model.sources.index("vocals")

    def iter_vocals(src, start, dur):
        """Yield (chunk_start_offset, mono 16 kHz vocals) chunk by chunk."""
        t = 0.0
        while t < dur - 0.01:
            d = min(CHUNK_SEC, dur - t)
            wav = read_audio(src, start + t, d, SR_DEMUCS, 2)
            x = torch.from_numpy(wav).to(device)
            ref = x.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                y = apply_model(model, ((x - mean) / std)[None], split=True, overlap=0.25,
                                progress=False, device=device)[0]
            voc = (y[vocals_idx] * std + mean).mean(0)
            voc = AF.resample(voc, SR_DEMUCS, SR_WHISPER)
            yield t, voc.cpu().numpy().astype(np.float32)
            t += d

    def separate(src, start, dur):
        parts = [v for _, v in iter_vocals(src, start, dur)]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    separate.iter = iter_vocals
    return separate


def separate_to_wav(separator, src, start, dur, path, log_every=1800):
    """Stream separated vocals of a long range into a 16 kHz mono WAV (bounded memory)."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR_WHISPER)
        done = 0.0
        for t, voc in separator.iter(src, start, dur):
            w.writeframes((np.clip(voc, -1, 1) * 32767).astype("<i2").tobytes())
            done = t + len(voc) / SR_WHISPER
            if int(done // log_every) != int(t // log_every):
                print(f"  人聲分離進度 {fmt_ts(done)} / {fmt_ts(dur)}")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config")
    ap.add_argument("--start", default="0")
    ap.add_argument("--end")
    args = ap.parse_args()
    need_tool("ffmpeg")
    from common import probe
    s = parse_ts(args.start)
    e = parse_ts(args.end) if args.end else probe(args.video)["duration"]
    sep = load_separator(load_config(args.config))
    print(f"分離人聲 {fmt_ts(s)}–{fmt_ts(e)} ...")
    separate_to_wav(sep, args.video, s, e - s, args.out)
    print(f"完成 → {args.out}")


if __name__ == "__main__":
    main()
