"""Transcribe a stream with faster-whisper (word timestamps) -> transcript.json / transcript.txt.

Usage:
  python transcribe.py --video STREAM.mp4 --workdir WORK [--config config.json]
                       [--model large-v3] [--start 0] [--end SECONDS] [--separate]

--separate: run Demucs vocal separation first (needs torch + demucs) and transcribe the vocals,
            which cuts most hallucinations caused by game music. Writes WORK/vocals16k.wav.
"""
import argparse
import os
import sys
import time
from pathlib import Path

from common import fmt_ts, load_config, parse_ts, probe, save_json, work_paths


def add_nvidia_dll_dirs():
    """On Windows, pip's nvidia-cublas/cudnn wheels put DLLs where ctranslate2 can't find them."""
    if os.name != "nt":
        return
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            for b in nv.glob("*/bin"):
                os.add_dll_directory(str(b))
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def get_converter(mode):
    if not mode:
        return lambda s: s
    try:
        import opencc
        for name in (mode, mode + ".json"):
            try:
                return opencc.OpenCC(name).convert
            except Exception:
                continue
        raise RuntimeError
    except Exception:
        print("[提示] 未安裝 opencc，略過簡轉繁。建議：pip install opencc-python-reimplemented")
        return lambda s: s


def make_text_fixer(cfg):
    """Chinese conversion + fixed replacements from config (e.g. a misheard streamer name)."""
    conv = get_converter(cfg.get("chinese_convert"))
    reps = [(k, v) for k, v in (cfg.get("text_replacements") or {}).items() if k and not k.startswith("_")]

    def fix(s):
        s = conv(s)
        for k, v in reps:
            s = s.replace(k, v)
        return s
    fix.conv, fix.reps = conv, reps
    return fix


def fix_words(words, reps):
    """Apply replacements across word tokens: large-v3 often emits one character per token,
    so "米" + "亞" must become "米" + "婭". Replaced text goes to the token where the match starts."""
    chars = [(ch, i) for i, w in enumerate(words) for ch in w["word"]]
    for k, v in reps:
        out, j = [], 0
        while j < len(chars):
            if "".join(c for c, _ in chars[j:j + len(k)]) == k:
                out += [(ch, chars[j][1]) for ch in v]
                j += len(k)
            else:
                out.append(chars[j])
                j += 1
        chars = out
    text = [""] * len(words)
    for ch, i in chars:
        text[i] += ch
    out = []
    for w, t in zip(words, text):
        if t:
            out.append(dict(w, word=t))
        elif out:  # token absorbed by a replacement: keep its timing on the previous token
            out[-1]["end"] = w["end"]
    return out


def load_model(cfg, model_name=None):
    add_nvidia_dll_dirs()
    from faster_whisper import WhisperModel

    wcfg = cfg["whisper"]
    model_name = model_name or wcfg["model"]
    device = wcfg.get("device", "auto")
    compute = wcfg.get("compute_type", "auto")
    if compute == "auto":
        try:
            import ctranslate2
            has_gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            has_gpu = False
        compute = "float16" if has_gpu else "int8"
        if not has_gpu:
            print("[提示] 沒偵測到 NVIDIA GPU，用 CPU 轉錄會很慢；長直播可改 --model medium 先試。")
    print(f"載入模型 {model_name}（{compute}）...")
    return WhisperModel(model_name, device=device, compute_type=compute)


def transcribe_kwargs(cfg):
    wcfg = cfg["whisper"]
    prompt = wcfg.get("initial_prompt", "")
    hot = [h for h in wcfg.get("hotwords", []) if h]
    if cfg.get("streamer_name") and "填這裡" not in cfg["streamer_name"]:
        hot.insert(0, cfg["streamer_name"])
    kwargs = dict(
        language=cfg.get("language", "zh"),
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=prompt or None,
        condition_on_previous_text=False,  # 降低長直播的幻覺/重複
        beam_size=5,
    )
    if hot:
        kwargs["hotwords"] = " ".join(hot)
    return kwargs


# Whisper's well-known Chinese hallucinations (subtitle-credit / video-outro text from its training
# data). Matched as substrings, so keep these specific enough to never hit real speech.
HALLUCINATIONS = ("請不吝點贊", "請不吝點讚", "明鏡與點點", "點點欄目", "字幕由", "字幕志願者",
                  "以下是臺灣中文", "以下是台灣中文")


def is_hallucination(text, prompt=None):
    t = text.strip()
    if any(h in t for h in HALLUCINATIONS):
        return True
    return bool(prompt) and len(t) >= 6 and t.rstrip("。.") in prompt


def run_whisper(model, audio, kwargs, conv, offset=0.0, progress=False):
    """Transcribe a path or 16 kHz numpy array; times are shifted by offset (source seconds)."""
    t0 = time.time()
    segments, info = model.transcribe(audio, **kwargs)
    prompt = conv(kwargs.get("initial_prompt") or "")
    out = []
    for seg in segments:
        text = conv(seg.text.strip())
        if not text or is_hallucination(text, prompt):
            continue
        wconv = getattr(conv, "conv", conv)
        words = [{"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": wconv(w.word)}
                 for w in (seg.words or [])]
        if getattr(conv, "reps", None):
            words = fix_words(words, conv.reps)
        out.append({"start": round(seg.start + offset, 3), "end": round(seg.end + offset, 3),
                    "text": text, "words": words})
        if progress and len(out) % 200 == 0:
            el = time.time() - t0
            print(f"  已轉錄到 {fmt_ts(seg.end + offset)} / {fmt_ts(info.duration + offset)}（耗時 {el/60:.1f} 分）")
    return out, info


def write_txt(segs, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(f"[{fmt_ts(s['start'])}] {s['text']}\n" for s in segs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--model")
    ap.add_argument("--start", default=None, help="只轉錄某段（測試用），秒數或 HH:MM:SS")
    ap.add_argument("--end", default=None)
    ap.add_argument("--separate", action="store_true", help="先用 Demucs 分離人聲再轉錄")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = work_paths(args.workdir)
    conv = make_text_fixer(cfg)
    kwargs = transcribe_kwargs(cfg)
    s = parse_ts(args.start) if args.start is not None else None
    e = parse_ts(args.end) if args.end else None

    t0 = time.time()
    audio, offset = args.video, 0.0
    if args.separate:
        from separate import load_separator, separate_to_wav
        start = s or 0.0
        end = e or probe(args.video)["duration"]
        print(f"人聲分離 {fmt_ts(start)}–{fmt_ts(end)}（Demucs）...")
        audio = str(separate_to_wav(load_separator(cfg), args.video, start, end - start,
                                    paths["dir"] / "vocals16k.wav"))
        offset = start  # the vocals file starts at `start`
        print(f"  人聲分離完成，耗時 {(time.time()-t0)/60:.1f} 分")
    elif s is not None or e is not None:
        kwargs["clip_timestamps"] = [s or 0.0, e] if e else [s or 0.0]

    model = load_model(cfg, args.model)
    out, info = run_whisper(model, audio, kwargs, conv, offset=offset, progress=True)
    save_json({"source": args.video, "duration": info.duration + offset, "separated": args.separate,
               "vocals_offset": offset, "segments": out}, paths["transcript_json"])
    write_txt(out, paths["transcript_txt"])
    print(f"完成：{len(out)} 段，耗時 {(time.time()-t0)/60:.1f} 分 → {paths['transcript_json']}")


if __name__ == "__main__":
    main()
