"""Run the whole analysis in one go, resumable, optionally ending with Claude's picks.

Usage:
  python pipeline.py --video STREAM.mp4 [--url YOUTUBE_URL | --chat-file live_chat.json]
                     [--workdir WORK] [--config config.json] [--start 1:00:00 --end 1:20:00]
                     [--model large-v3-turbo] [--no-separate] [--force] [--auto-select]

Steps: transcribe / chat / audio / visual run in parallel, then vocal -> signals -> frames.
A step whose output already exists is skipped (--force reruns everything), and a failed step is
logged and the rest carry on (e.g. no chat replay). Each step's output goes to WORK/logs/<step>.log.

--auto-select: afterwards run Claude Code headless (`claude -p`, uses the Claude subscription,
no API key) to do SKILL.md step 3 unattended: it writes candidates_review.md and
selections.draft.json, and never renders clips.

Progress is printed as lines starting with "@@ " followed by JSON, for the Clip Review app:
  @@ {"step": "transcribe", "status": "start" | "done" | "skip" | "fail", "sec": 12.3}
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common import SKILL_DIR, default_workdir, find_config, load_config, load_json, work_paths

SCRIPTS = SKILL_DIR / "scripts"
PY = sys.executable


def emit(log, step, status, **extra):
    rec = {"step": step, "status": status, **extra}
    print("@@ " + json.dumps(rec, ensure_ascii=False), flush=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {step} {status} "
                f"{json.dumps(extra, ensure_ascii=False) if extra else ''}\n")


def has_separation():
    return all(importlib.util.find_spec(m) for m in ("torch", "demucs"))


class Step:
    def __init__(self, name, cmd, output, cwd=SCRIPTS):
        self.name, self.cmd, self.output, self.cwd = name, cmd, output, cwd

    def done(self):
        p = Path(self.output)
        if p.is_dir():
            return any(p.iterdir())
        return p.exists() and p.stat().st_size > 0


def start(step, logdir):
    f = open(logdir / f"{step.name}.log", "w", encoding="utf-8")
    proc = subprocess.Popen(step.cmd, cwd=step.cwd, stdout=f, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return proc, f, time.time()


def run_group(steps, args, log, logdir):
    """Run steps in parallel; return names of the ones that failed."""
    running, failed = [], []
    for s in steps:
        if s is None:
            continue
        if s.done() and not args.force:
            emit(log, s.name, "skip")
            continue
        emit(log, s.name, "start")
        running.append((s, *start(s, logdir)))
    for s, proc, f, t0 in running:
        rc = proc.wait()
        f.close()
        sec = round(time.time() - t0, 1)
        if rc == 0:
            emit(log, s.name, "done", sec=sec)
        else:
            tail = (logdir / f"{s.name}.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            emit(log, s.name, "fail", sec=sec, error=" / ".join(tail))
            failed.append(s.name)
    return failed


AUTO_PROMPT = """你在「無人在線」模式執行 stream-highlights-pipeline skill（說明在 {skill}/SKILL.md）。
直播檔：{video}
工作資料夾 WORK：{work}
設定檔：{config}

分析（第 2 步）已經跑完，請做 SKILL.md 第 3 步「挑選」：讀 references/client_style.md（沒有就讀 client_style.example.md）、references/selection_guide.md、
WORK/candidates_context.md、WORK/transcript.txt、WORK/frames/*.jpg。長度遵守上述風格檔。
WORK 裡如果已經有 selections.json，那是使用者之前的選擇，可以參考但不要修改。

只能執行這兩種指令（請照抄格式，從目前資料夾執行，只改時間或檔名）：
- 補截圖：python scripts/frames.py --video "{video}" --workdir "{work}" --times 1:23:45 2:34:56
- 檢查 JSON：python -m json.tool "{work}/selections.draft.json"

完成後只寫兩個檔案就結束：
1. WORK/candidates_review.md：候選清單（格式見 SKILL.md）。
2. WORK/selections.draft.json：同 selections.json 的格式（見 scripts/cut_clips.py 開頭），收錄你推薦的每一支，
   每支額外加 "summary"（一句話）與 "confidence"（高/中/低）欄位。寫完用上面的指令檢查。
不要執行 refine_transcript.py、cut_clips.py、export_premiere_xml.py，不要出片。"""


def find_claude():
    """`claude` on PATH, or the native installer's default location (often not on PATH)."""
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if p.exists():
            return str(p)
    return None


def auto_select(args, paths, log):
    claude = find_claude()
    if not claude:
        emit(log, "auto_select", "fail", error="找不到 claude 指令（Claude Code）")
        return False
    if paths["draft"].exists() and not args.force:
        emit(log, "auto_select", "skip")
        return True
    prompt = AUTO_PROMPT.format(skill=SKILL_DIR, video=args.video, work=paths["dir"].resolve(),
                                config=args.config or "（無，使用預設）")
    # prompt goes in on stdin: --add-dir / --allowedTools take several values and would swallow it
    cmd = [claude, "-p", "--permission-mode", "acceptEdits",
           "--add-dir", str(paths["dir"].resolve()),
           "--allowedTools", "Read,Glob,Grep,Write,Edit,Bash(python scripts/frames.py:*),Bash(python -m json.tool:*)"]
    emit(log, "auto_select", "start")
    t0 = time.time()
    with open(paths["dir"] / "logs" / "auto_select.log", "w", encoding="utf-8") as f:
        rc = subprocess.run(cmd, cwd=SKILL_DIR, input=prompt, text=True, encoding="utf-8",
                            stdout=f, stderr=subprocess.STDOUT).returncode
    ok = rc == 0 and paths["review"].exists()
    err = {}
    if not ok:
        text = (paths["dir"] / "logs" / "auto_select.log").read_text(encoding="utf-8", errors="replace")
        if "Not logged in" in text or "/login" in text:
            err["error"] = "Claude Code 尚未登入：在終端機執行一次 claude 並登入後再重跑"
        else:
            err["error"] = "Claude 沒有產生 candidates_review.md，請看 logs/auto_select.log"
    emit(log, "auto_select", "done" if ok else "fail", sec=round(time.time() - t0, 1), **err)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--workdir", help="預設：直播檔旁邊的 <日期資料夾名>_精華/（不在日期資料夾就用 <檔名>_精華/）")
    ap.add_argument("--config", help="預設：從工作資料夾、直播檔資料夾往上兩層找 config.json")
    ap.add_argument("--url", help="YouTube 存檔網址（抓聊天室）")
    ap.add_argument("--chat-file", help="已下載的 live_chat.json")
    ap.add_argument("--cookies-from-browser")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--model", default="large-v3-turbo", help="整場掃描用的轉錄模型")
    ap.add_argument("--no-separate", action="store_true", help="不做人聲分離")
    ap.add_argument("--frames", type=int, default=20, help="候選截圖張數")
    ap.add_argument("--force", action="store_true", help="全部重跑")
    ap.add_argument("--auto-select", action="store_true", help="分析完讓 Claude 挑選（不出片）")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        raise SystemExit(f"[錯誤] 找不到直播檔：{video}")
    args.video = str(video)
    work = Path(args.workdir) if args.workdir else default_workdir(video)
    if not args.config:
        args.config = find_config(work, video.parent)
    load_config(args.config)  # fail early on a broken config
    paths = work_paths(work)
    logdir = paths["dir"] / "logs"
    logdir.mkdir(exist_ok=True)
    log = paths["pipeline_log"]
    emit(log, "pipeline", "start", workdir=str(paths["dir"].resolve()), config=args.config)

    base = ["--workdir", str(paths["dir"])]
    cfg = ["--config", args.config] if args.config else []
    rng = (["--start", args.start] if args.start else []) + (["--end", args.end] if args.end else [])

    separate = not args.no_separate and has_separation()
    tr = [PY, "transcribe.py", "--video", args.video, *base, *cfg, "--model", args.model, *rng]
    if separate:
        tr.append("--separate")
    chat = None
    if args.chat_file:
        chat = [PY, "fetch_chat.py", "--file", args.chat_file, *base]
    elif args.url:
        chat = [PY, "fetch_chat.py", "--url", args.url, *base]
        if args.cookies_from_browser:
            chat += ["--cookies-from-browser", args.cookies_from_browser]
    failed = run_group([
        Step("transcribe", tr, paths["transcript_json"]),
        Step("chat", chat, paths["chat"]) if chat else None,
        Step("audio", [PY, "audio_energy.py", "--video", args.video, *base], paths["audio"]),
        Step("visual", [PY, "visual_events.py", "--video", args.video, *base, *cfg, *rng], paths["visual"]),
    ], args, log, logdir)
    if not chat:
        emit(log, "chat", "skip", reason="沒有提供 --url 或 --chat-file")
    failed += run_group([Step("vocal", [PY, "vocal_events.py", *base, "--video", args.video, *rng],
                              paths["vocal"])], args, log, logdir)

    # the readable transcript is what Claude reads in step 3; rebuild it if only the JSON is there
    if paths["transcript_json"].exists() and not paths["transcript_txt"].exists():
        from transcribe import write_txt
        write_txt(load_json(paths["transcript_json"])["segments"], paths["transcript_txt"])

    # signals and frames depend on everything above, so always rebuild them when anything ran
    sig = Step("signals", [PY, "signals.py", *base, *cfg, "--video", args.video], paths["candidates"])
    frm = Step("frames", [PY, "frames.py", "--video", args.video, *base, "--candidates",
                          "--limit", str(args.frames)], paths["frames"])
    always = argparse.Namespace(force=True)
    failed += run_group([sig], always, log, logdir)
    if "signals" not in failed:
        failed += run_group([frm], always, log, logdir)

    if args.auto_select and "signals" not in failed:
        if not auto_select(args, paths, log):
            failed.append("auto_select")
    emit(log, "pipeline", "done", failed=failed, workdir=str(paths["dir"].resolve()))
    sys.exit(1 if "signals" in failed else 0)


if __name__ == "__main__":
    main()
