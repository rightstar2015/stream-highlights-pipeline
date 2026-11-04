"""Shared helpers for the stream-highlights-pipeline scripts."""
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

# Windows consoles default to cp950; force UTF-8 so Chinese prints safely.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_DIR = Path(__file__).resolve().parent.parent


def load_json(path):
    # utf-8-sig: Notepad and PowerShell often save UTF-8 with a BOM
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


@contextmanager
def atomic_output(path):
    """A temporary path next to `path` that replaces it only when the block finishes. A run that is
    stopped or crashes then never leaves a half-written file that looks finished. The extension is
    kept, since ffmpeg picks the format from it."""
    path = Path(path)
    tmp = path.with_name(f"{path.stem}.part{path.suffix}")
    try:
        yield tmp
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_write(path, encoding="utf-8"):
    """open(path, "w") that only replaces path once everything is written."""
    with atomic_output(path) as tmp, open(tmp, "w", encoding=encoding) as f:
        yield f


def save_json(obj, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with atomic_write(path) as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_config(path=None):
    """Load config.json over its language profile (languages/*.json) over config.example.json."""
    base = load_json(SKILL_DIR / "config.example.json")
    user = load_json(path) if path and Path(path).exists() else {}
    name = user.get("language_profile", base["language_profile"])
    profile = SKILL_DIR / "languages" / f"{Path(name).name}.json"
    if not profile.is_file():
        sys.exit(f"[錯誤] 找不到語言設定檔：{profile}")
    _deep_merge(base, load_json(profile))
    _deep_merge(base, user)
    return base


def find_config(*dirs, levels=2):
    """First config.json found in each dir or up to `levels` parents above it.
    The usual layout is <media root>/config.json with streams in <media root>/<date>/."""
    for d in dirs:
        p = Path(d).resolve()
        for _ in range(levels + 1):
            if (p / "config.json").is_file():
                return str(p / "config.json")
            p = p.parent
    return None


def default_workdir(video):
    """<date>_精華 when the stream sits in a date folder (<media root>/20260101/), else <file name>_精華."""
    video = Path(video).resolve()
    folder = video.parent.name
    stem = folder if folder.isdigit() and len(folder) == 8 else video.stem
    return video.parent / f"{stem}_精華"


def _deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def fmt_ts(sec, ms=False):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    if ms:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    return f"{h:02d}:{m:02d}:{int(s):02d}"


def parse_ts(value):
    """Accept seconds (number/str) or HH:MM:SS(.ms) / MM:SS."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def normalize_segments(clip):
    """Segments may be [in, out] or {"in": .., "out": .., "crop": "face"}.
    Returns list of dicts with float a/b and optional crop."""
    out = []
    for s in clip["segments"]:
        if isinstance(s, dict):
            out.append({"a": parse_ts(s["in"]), "b": parse_ts(s["out"]), "crop": s.get("crop")})
        else:
            out.append({"a": parse_ts(s[0]), "b": parse_ts(s[1]), "crop": None})
    return out


def source_to_clip_time(segs, t):
    """Map a source-time to the clip's output timeline (None if cut out)."""
    off = 0.0
    for s in segs:
        if s["a"] <= t <= s["b"]:
            return off + (t - s["a"])
        off += s["b"] - s["a"]
    return None


def need_tool(name):
    if shutil.which(name) is None:
        sys.exit(f"[錯誤] 找不到 {name}，請先安裝（執行 setup_check.py 看安裝指令）。")


def run(cmd, quiet=True):
    """Run a command, raising with stderr on failure."""
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"指令失敗：{' '.join(map(str, cmd[:6]))} ...\n{tail}")
    if not quiet:
        print(res.stdout)
    return res


def probe(path):
    """Return basic stream info for a media file."""
    need_tool("ffprobe")
    res = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", str(path)])
    data = json.loads(res.stdout)
    info = {"duration": float(data["format"].get("duration", 0))}
    for st in data["streams"]:
        if st["codec_type"] == "video" and "width" not in info:
            num, den = (st.get("r_frame_rate") or "30/1").split("/")
            num, den = int(num), int(den or 1)
            info.update(width=int(st["width"]), height=int(st["height"]),
                        fps_num=num, fps_den=den, fps=num / den)
        elif st["codec_type"] == "audio" and "audio_channels" not in info:
            info.update(audio_channels=int(st.get("channels", 2)),
                        audio_rate=int(st.get("sample_rate", 48000)))
    return info


def work_paths(workdir):
    w = Path(workdir)
    w.mkdir(parents=True, exist_ok=True)
    return {
        "dir": w,
        "transcript_json": w / "transcript.json",
        "transcript_txt": w / "transcript.txt",
        "dropped_txt": w / "transcript_dropped.txt",
        "refined_json": w / "transcript_refined.json",
        "refined_txt": w / "transcript_refined.txt",
        "chat": w / "chat.jsonl",
        "audio": w / "audio_energy.csv",
        "visual": w / "visual_events.csv",
        "vocal": w / "vocal_events.csv",
        "vocals_wav": w / "vocals16k.wav",
        "signals": w / "signals.csv",
        "candidates": w / "candidates.json",
        "context": w / "candidates_context.md",
        "frames": w / "frames",
        "selections": w / "selections.json",
        "draft": w / "selections.draft.json",
        "review": w / "candidates_review.md",
        "pipeline_log": w / "pipeline_log.txt",
        "clips": w / "clips",
    }


def load_transcript(path):
    if not Path(path).exists():
        return []
    return load_json(path).get("segments", [])


def merge_windows(ranges, pad, duration=None, join_gap=None):
    """Pad source ranges [(a, b), ...] and merge overlapping/near ones -> sorted [[a, b], ...]."""
    join_gap = 2 * pad if join_gap is None else join_gap
    out = []
    for a, b in sorted((max(0.0, a - pad), b + pad) for a, b in ranges):
        if duration is not None:
            b = min(b, duration)
        if out and a <= out[-1][1] + join_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def merge_transcripts(base, refined):
    """Replace base segments inside the refined windows with the refined segments.

    refined: {"windows": [[a, b], ...], "segments": [...]} as written by refine_transcript.py.
    A base segment is dropped when its midpoint falls inside any window.
    """
    wins = refined.get("windows", [])

    def inside(s):
        mid = (s["start"] + s["end"]) / 2
        return any(a <= mid <= b for a, b in wins)

    kept = [s for s in base if not inside(s)]
    return sorted(kept + refined.get("segments", []), key=lambda s: s["start"])


def load_best_transcript(paths):
    """Base transcript with refined windows (if any) swapped in."""
    base = load_transcript(paths["transcript_json"])
    if Path(paths["refined_json"]).exists():
        return merge_transcripts(base, load_json(paths["refined_json"]))
    return base


def load_chat(path):
    msgs = []
    if Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    msgs.append(json.loads(line))
    return msgs
