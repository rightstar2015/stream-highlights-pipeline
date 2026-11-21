"""OrangeLink extension: runs the analysis for a video OrangeLink hands over and answers with
highlight candidates. The manifest is orangelink-extension.json next to this file; the protocol
is OrangeLink's docs/EXTENSIONS.md.

Usage (OrangeLink runs these):
  python orangelink_adapter.py find REQUEST.json RESULT.json
  python orangelink_adapter.py check

find runs scripts/pipeline.py on the video (resumable, so a stream analysed before reuses its
work folder) and answers with Claude's picks (selections.draft.json) when "Claude 挑選" is on
and succeeded, else with the signal candidates (candidates.json). Nothing is rendered.
Progress goes to stdout as OrangeLink's `@@ {"progress": .., "message": ..}` lines.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
sys.path.insert(0, str(SCRIPTS))
from common import default_workdir, find_config, load_config, load_json, normalize_segments, save_json  # noqa: E402

PROTOCOL = 1
STEPS = {"transcribe": "轉錄", "chat": "聊天室", "audio": "音量", "visual": "畫面分析", "vocal": "尖叫／笑聲",
         "signals": "合併訊號", "frames": "截圖", "auto_select": "Claude 挑選"}
# Seconds kept around a signal peak when there are no picks: the same as cut windows in SKILL.md step 2.
BEFORE, AFTER = 20.0, 10.0
NAME_MAX, NOTE_MAX, CLIPS_MAX = 80, 2000, 200


def say(progress=None, message=None):
    rec = {k: v for k, v in (("progress", progress), ("message", message)) if v is not None}
    print("@@ " + json.dumps(rec, ensure_ascii=False), flush=True)


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def crop_center(crop, layout, width):
    """A vertical clip's crop as OrangeLink's crop_x (the 9:16 frame's centre, 0..1), or None."""
    if not width:
        return None
    if isinstance(crop, str) or crop is None:
        crop = layout.get("crop_presets", {}).get(crop or layout.get("default_crop", "game"))
    if isinstance(crop, dict):
        crop = crop.get("region")
    if not (isinstance(crop, list) and len(crop) == 4):
        return None
    x, _, w, _ = (float(v) for v in crop)
    return min(1.0, max(0.0, (x + w / 2) / width))


def from_draft(draft, layout, width, duration):
    """Claude's picks: one candidate per clip, spanning its first to last segment."""
    out = []
    for c in draft.get("clips", []):
        segs = normalize_segments(c)
        if not segs:
            continue
        a, b = segs[0]["a"], segs[-1]["b"]
        if duration:
            b = min(b, duration)
        if not (0 <= a < b):
            continue
        kind = "直式" if c.get("format") == "vertical" else "橫式"
        lines = [f"Claude 推薦（{kind}）"]
        if len(segs) > 1:
            lines.append("跳剪：" + "、".join(f"{fmt(s['a'])}–{fmt(s['b'])}" for s in segs))
        if c.get("header_title"):
            lines.append(f"標題條：{c['header_title']}")
        lines += [f"{n.get('at', '')} {n.get('text', '')}".strip() for n in c.get("notes", []) if isinstance(n, dict)]
        clip = {"start": round(a, 2), "end": round(b, 2), "note": "\n".join(lines)[:NOTE_MAX]}
        if c.get("title"):
            clip["name"] = str(c["title"])[:NAME_MAX]
        if c.get("format") == "vertical":
            x = crop_center(segs[0].get("crop"), layout, width)
            if x is not None:
                clip["crop_x"] = round(x, 3)
        out.append(clip)
    return out


def from_candidates(cands, duration):
    """Signal peaks, best first in the score, as windows around each peak."""
    out = []
    for c in cands.get("candidates", []):
        peak = float(c["peak"])
        a = max(0.0, peak - BEFORE)
        b = peak + AFTER
        if duration:
            b = min(b, duration)
        if not a < b:
            continue
        parts = [f"{k} {v:+.1f}" for k, v in sorted(c.get("breakdown", {}).items(), key=lambda kv: -kv[1]) if v > 0.5][:4]
        note = f"訊號第 {c.get('rank', '?')} 名，分數 {c['score']}" + (f"（{'、'.join(parts)}）" if parts else "")
        if c.get("chat_in_window"):
            note += f"，聊天 {c['chat_in_window']} 則"
        out.append({"start": round(a, 2), "end": round(b, 2), "note": note, "score": float(c["score"])})
    return out


def find(request_path, result_path):
    req = load_json(request_path)
    if req.get("protocol") != PROTOCOL:
        raise SystemExit(f"不支援的 OrangeLink 協定：{req.get('protocol')}")
    item, opts = req["item"], req.get("options", {})
    video = Path(item.get("original") or item["path"])
    # The work folder the pipeline would pick on its own, so Clip Review and earlier runs share it.
    work = default_workdir(video) if item.get("original") else Path(req["workdir"])
    cmd = [sys.executable, "pipeline.py", "--video", str(video), "--workdir", str(work)]
    if item.get("chat_file"):
        cmd += ["--chat-file", item["chat_file"]]
    if not opts.get("separate", True):
        cmd.append("--no-separate")
    auto = bool(opts.get("auto-select"))
    if auto:
        cmd.append("--auto-select")
    total = 6 + auto
    done, error, failed = 0, None, []
    say(0.0, "開始分析")
    proc = subprocess.Popen(cmd, cwd=SCRIPTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    for line in proc.stdout:
        line = line.rstrip()
        if not line.startswith("@@ "):
            print(line, flush=True)
            continue
        try:
            ev = json.loads(line[3:])
        except ValueError:
            continue
        step, status = ev.get("step"), ev.get("status")
        if step == "pipeline":
            failed = ev.get("failed", failed)
            continue
        if status == "fail":
            error = ev.get("error") or f"{STEPS.get(step, step)}失敗"
            print(f"{STEPS.get(step, step)}：{error}", flush=True)
        if status in ("done", "skip", "fail") and step in STEPS:
            done += 1
        name = STEPS.get(step, step)
        word = {"start": "中…", "done": "完成", "skip": "沿用", "fail": "失敗"}.get(status, status or "")
        say(min(done / total, 0.99), f"{name}{word}")
    rc = proc.wait()
    if rc != 0:
        # The last plain line is what OrangeLink shows.
        print(error or f"分析失敗（代碼 {rc}），請看 {work / 'logs'}", flush=True)
        sys.exit(1)
    config = load_config(find_config(work, video.parent))
    duration = item.get("duration")
    paths_draft, paths_cands = work / "selections.draft.json", work / "candidates.json"
    message = None
    if auto and "auto_select" not in failed and paths_draft.exists():
        clips = from_draft(load_json(paths_draft), config["vertical_layout"], item.get("width"), duration)
        message = f"Claude 推薦 {len(clips)} 支"
    else:
        if not paths_cands.exists():
            print("沒有產生 candidates.json", flush=True)
            sys.exit(1)
        clips = from_candidates(load_json(paths_cands), duration)
        if auto:
            message = "Claude 挑選沒有完成，改用訊號候選"
    clips = sorted(clips, key=lambda c: c["start"])[:CLIPS_MAX]
    save_json({"protocol": PROTOCOL, "clips": clips, **({"message": message} if message else {})}, result_path)
    say(1.0, "完成")


def check():
    r = subprocess.run([sys.executable, "setup_check.py"], cwd=SCRIPTS, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(r.stdout + r.stderr, end="")
    missing = r.returncode != 0 or any(line.lstrip().startswith("❌") for line in r.stdout.splitlines())
    sys.exit(1 if missing else 0)


def main():
    args = sys.argv[1:]
    if args[:1] == ["find"] and len(args) == 3:
        find(Path(args[1]), Path(args[2]))
    elif args == ["check"]:
        check()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
