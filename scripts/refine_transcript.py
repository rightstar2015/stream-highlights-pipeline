"""Re-transcribe only the selected segments at high quality: vocal separation + a bigger model.

Usage:
  python refine_transcript.py --workdir WORK [--config config.json] [--only h01 h02]
                              [--model large-v3] [--pad 3] [--no-separate]

Reads WORK/selections.json. Every segment is padded by --pad seconds and nearby ones are merged
into windows; each window is separated with Demucs (whisper.refine_separate) and transcribed
with whisper.refine_model. Writes:
  transcript_refined.json  {"windows": [[a, b], ...], "model": .., "separated": .., "segments": [...]}
  transcript_refined.txt   the full transcript with refined windows swapped in (for reading)
transcript.json is left untouched. cut_clips.py uses the refined text automatically when present.
Run cut_clips.py again afterwards to rebuild the SRT files.
"""
import argparse
import time

from common import (fmt_ts, load_config, load_json, load_transcript, merge_transcripts,
                    merge_windows, need_tool, normalize_segments, probe, save_json, work_paths)
from transcribe import load_model, make_text_fixer, run_whisper, transcribe_kwargs, write_txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--model")
    ap.add_argument("--pad", type=float, default=3.0, help="每段前後多轉幾秒（避免句子被切在邊界）")
    ap.add_argument("--no-separate", action="store_true", help="不做人聲分離")
    args = ap.parse_args()
    need_tool("ffmpeg")

    cfg = load_config(args.config)
    wcfg = cfg["whisper"]
    paths = work_paths(args.workdir)
    sel = load_json(paths["selections"])
    src = sel["source"]
    dur = probe(src)["duration"]

    ranges = []
    for clip in sel["clips"]:
        if args.only and clip["id"] not in args.only:
            continue
        ranges += [(s["a"], s["b"]) for s in normalize_segments(clip)]
    windows = merge_windows(ranges, args.pad, dur)
    # keep earlier refined windows from other runs (e.g. --only on a different clip)
    old = load_json(paths["refined_json"]) if paths["refined_json"].exists() else {}
    total = sum(b - a for a, b in windows)
    separate_on = wcfg.get("refine_separate", True) and not args.no_separate
    model_name = args.model or wcfg.get("refine_model", "large-v3")
    print(f"重轉 {len(windows)} 個區間，共 {total/60:.1f} 分鐘；模型 {model_name}；"
          f"人聲分離 {'開' if separate_on else '關'}")

    separator = None
    if separate_on:
        from separate import load_separator
        separator = load_separator(cfg)
    model = load_model(cfg, model_name)
    kwargs = transcribe_kwargs(cfg)
    # Keep VAD on: without it, music/silence at a window start makes Whisper echo the prompt and
    # skip the next 20-40 s. Prompt and hotwords are off by default for the same reason.
    if not wcfg.get("refine_use_prompt", False):
        kwargs.pop("initial_prompt", None)
        kwargs.pop("hotwords", None)
    conv = make_text_fixer(cfg)
    base = load_transcript(paths["transcript_json"])

    def chars_in(segs, a, b):
        return sum(len(s["text"]) for s in segs if a <= (s["start"] + s["end"]) / 2 <= b)

    t0 = time.time()
    new_segs, done_windows = [], []
    for i, (a, b) in enumerate(windows, 1):
        if separator:
            audio = separator(src, a, b - a)
        else:
            from separate import SR_WHISPER, read_audio
            audio = read_audio(src, a, b - a, SR_WHISPER, 1)[0]
        segs, _ = run_whisper(model, audio, kwargs, conv, offset=a, limits=wcfg.get("filter"))
        segs = [s for s in segs if s["start"] < b]
        n_new, n_old = chars_in(segs, a, b), chars_in(base, a, b)
        if n_old >= 10 and n_new < 0.5 * n_old:
            # the refined pass probably lost speech; keep the original text for this window
            print(f"  [{i}/{len(windows)}] {fmt_ts(a)}–{fmt_ts(b)}：重轉只有 {n_new} 字（原本 {n_old} 字），"
                  "保留原逐字稿，請人工確認")
            continue
        new_segs += segs
        done_windows.append([a, b])
        print(f"  [{i}/{len(windows)}] {fmt_ts(a)}–{fmt_ts(b)}：{len(segs)} 句（{n_old} → {n_new} 字）")

    win_all = merge_windows([tuple(w) for w in old.get("windows", [])] + [tuple(w) for w in done_windows], 0)
    old_kept = [s for s in old.get("segments", [])
                if not any(a <= (s["start"] + s["end"]) / 2 <= b for a, b in done_windows)]
    refined = {"windows": win_all, "model": model_name, "separated": separate_on,
               "segments": sorted(old_kept + new_segs, key=lambda s: s["start"])}
    save_json(refined, paths["refined_json"])
    write_txt(merge_transcripts(load_transcript(paths["transcript_json"]), refined), paths["refined_txt"])
    print(f"完成，耗時 {(time.time()-t0)/60:.1f} 分 → {paths['refined_json']}")
    print("接著重跑 cut_clips.py（和 export_premiere_xml.py）就會用新的字幕。")


if __name__ == "__main__":
    main()
