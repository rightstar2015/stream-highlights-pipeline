"""Clip Review: a local web UI over the stream-highlights-pipeline scripts.

Usage: python app/server.py [--port 8765] [--no-browser]     (or double-click start_clip_review.bat)

Standard library only. Binds to 127.0.0.1. One background job (analysis or render) at a time;
the page polls /api/job for progress. All the real work is done by scripts/*.py.

Websites open in the same browser can send requests to localhost, so every request has to pass
check_request (Host / Origin / Fetch Metadata), POST bodies must be application/json, and
project-specific endpoints only accept folders that are already known projects.
"""
import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

APP = Path(__file__).resolve().parent
SKILL = APP.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
from common import default_workdir, find_config, fmt_ts, load_json, save_json, work_paths  # noqa: E402

PY = sys.executable
SETTINGS = Path.home() / ".clip-review.json"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
REVIEW_STATE = "review_state.json"  # every clip on the review page, checked or not, with unsaved edits
PROJECT_MARKERS = ("candidates.json", "studio.json", "selections.json")


def settings():
    try:
        return load_json(SETTINGS)
    except Exception:
        return {"recent": []}


def remember(work, video):
    s = settings()
    rec = [r for r in s.get("recent", []) if r["work"] != work]
    s["recent"] = [{"work": work, "video": video}] + rec[:9]
    save_json(s, SETTINGS)


# ---------------------------------------------------------------- background job

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.kind, self.running, self.rc = None, False, None
        self.events, self.log, self.work = [], [], None

    def snapshot(self):
        with self.lock:
            return {"kind": self.kind, "running": self.running, "rc": self.rc, "work": self.work,
                    "events": self.events[-50:], "log": self.log[-80:]}

    def start(self, kind, work, cmds):
        """cmds: list of (label, argv); run in order, stop at the first failure."""
        with self.lock:
            if self.running:
                return False
            self.kind, self.running, self.rc, self.work = kind, True, None, work
            self.events, self.log = [], []
        threading.Thread(target=self._run, args=(cmds,), daemon=True).start()
        return True

    def _run(self, cmds):
        rc = 0
        for label, argv in cmds:
            if label:
                self._event({"step": label, "status": "start"})
            p = subprocess.Popen(argv, cwd=SCRIPTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", env=ENV)
            for line in p.stdout:
                line = line.rstrip()
                if line.startswith("@@ "):
                    try:
                        self._event(json.loads(line[3:]))
                        continue
                    except ValueError:
                        pass
                with self.lock:
                    self.log.append(line)
            rc = p.wait()
            if label:
                self._event({"step": label, "status": "done" if rc == 0 else "fail"})
            if rc != 0:
                break
        with self.lock:
            self.running, self.rc = False, rc

    def _event(self, e):
        with self.lock:
            self.events.append(e)


JOB = Job()


# ---------------------------------------------------------------- project data

def studio_meta(paths):
    p = paths["dir"] / "studio.json"
    return load_json(p) if p.exists() else {}


def frame_for(paths, peak):
    """frames.py --candidates names strips after (peak - 8)."""
    name = fmt_ts(max(0, peak - 8)).replace(":", "-") + ".jpg"
    return str(paths["frames"] / name) if (paths["frames"] / name).exists() else None


def project_state(work):
    paths = work_paths(work)
    meta = studio_meta(paths)
    cands = load_json(paths["candidates"])["candidates"] if paths["candidates"].exists() else []
    for c in cands:
        c["frame"] = frame_for(paths, c["peak"])
    state_file = paths["dir"] / REVIEW_STATE
    out = {
        "review_state": load_json(state_file) if state_file.exists() else None,
        "draft_newer": (paths["draft"].exists() and state_file.exists()
                        and paths["draft"].stat().st_mtime > state_file.stat().st_mtime),
        "work": str(paths["dir"].resolve()),
        "video": meta.get("video"),
        "config": meta.get("config"),
        "candidates": cands,
        "draft": load_json(paths["draft"]) if paths["draft"].exists() else None,
        "selections": load_json(paths["selections"]) if paths["selections"].exists() else None,
        "review": paths["review"].read_text(encoding="utf-8") if paths["review"].exists() else None,
        "clips": [],
    }
    if paths["clips"].exists():
        out["clips"] = sorted(str(p) for p in paths["clips"].iterdir() if p.suffix in (".mp4", ".srt"))
    out["xml"] = [str(p) for p in paths["dir"].glob("*_premiere.xml")]
    return out


def make_preview(work, video, ranges):
    """Low-res proxy of the clip's segments joined in order (jump cuts), so the browser never
    has to read the multi-hour source."""
    paths = work_paths(work)
    d = paths["dir"] / "previews"
    d.mkdir(exist_ok=True)
    out = d / ("_".join(f"{a:.1f}-{b:.1f}" for a, b in ranges)[:150] + ".mp4")
    if not out.exists():
        cmd = ["ffmpeg", "-y", "-v", "error"]
        for a, b in ranges:  # one seeked input per segment keeps each seek fast
            cmd += ["-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", video]
        n = len(ranges)
        fl = "".join(f"[{i}:v]scale=-2:480,setsar=1[v{i}];" for i in range(n))
        fl += "".join(f"[v{i}][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
        cmd += ["-filter_complex", fl, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
    return str(out)


def pick_path(kind):
    """Native file/folder dialog, in a subprocess so tkinter gets its own main thread."""
    code = ("import tkinter as tk; from tkinter import filedialog as fd; r = tk.Tk(); r.withdraw(); "
            "r.attributes('-topmost', True); "
            + ("p = fd.askdirectory()" if kind == "dir" else
               "p = fd.askopenfilename(filetypes=[('影片/聊天室/設定', '*.mp4 *.mkv *.mov *.flv *.json'), ('全部', '*.*')])")
            + "; print(p or '')")
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, encoding="utf-8", env=ENV)
    return r.stdout.strip()


def allowed_file(path):
    """Only serve files inside a project folder (or this app) — the server is local, but be tidy."""
    p = Path(path).resolve()
    roots = [APP] + [Path(r["work"]).resolve() for r in settings().get("recent", [])]
    return any(p == r or r in p.parents for r in roots) and p.is_file()


def known_project(work):
    """A folder the user already opened or analysed in this app."""
    p = Path(work).resolve()
    return any(p == Path(r["work"]).resolve() for r in settings().get("recent", []))


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def check_request(self, path):
        """Refuse requests that other websites could make through the user's browser.
        Host stops DNS rebinding, Origin stops cross-site POSTs, Fetch Metadata stops cross-site GETs.
        The page itself may be linked from elsewhere; it holds no data."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            return False
        if path in ("/", "/index.html"):
            return True
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self.check_request(u.path):
            return self.send_json({"error": "拒絕來自其他網站的請求"}, 403)
        if u.path in ("/", "/index.html"):
            return self.send_file(APP / "static" / "index.html")
        if u.path == "/api/settings":
            return self.send_json(settings())
        if u.path == "/api/job":
            return self.send_json(JOB.snapshot())
        if u.path == "/api/project":
            if not q.get("work") or not Path(q["work"]).exists():
                return self.send_json({"error": "找不到工作資料夾"}, 404)
            if not any((Path(q["work"]) / m).exists() for m in PROJECT_MARKERS):
                return self.send_json({"error": "這個資料夾不是專案（沒有分析資料）"}, 400)
            state = project_state(q["work"])
            remember(state["work"], state["video"])  # also lets /file serve its frames and clips
            return self.send_json(state)
        if u.path == "/file":
            p = q.get("path", "")
            if not allowed_file(p):
                return self.send_json({"error": "不允許的檔案"}, 403)
            return self.send_file(Path(p))
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self.check_request(u.path):
            return self.send_json({"error": "拒絕來自其他網站的請求"}, 403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send_json({"error": "只接受 application/json"}, 415)
        try:
            data = self.body()
            handler = {"/api/analyze": self.analyze, "/api/render": self.render,
                       "/api/preview": self.preview, "/api/pick": self.pick,
                       "/api/open": self.open_path, "/api/review_state": self.review_state}.get(u.path)
            if not handler:
                return self.send_json({"error": "not found"}, 404)
            if u.path in ("/api/render", "/api/preview", "/api/review_state") and not known_project(data.get("work", "")):
                return self.send_json({"error": "不是已開啟的專案"}, 403)
            return handler(data)
        except Exception as e:  # report to the page instead of dropping the connection
            return self.send_json({"error": str(e)}, 500)

    def analyze(self, d):
        video = Path(d["video"]).resolve()
        if not video.is_file():
            return self.send_json({"error": f"找不到直播檔：{video}"}, 400)
        work = Path(d.get("work") or default_workdir(video)).resolve()
        if work.parent == work:
            return self.send_json({"error": "工作資料夾不能是磁碟根目錄"}, 400)
        paths = work_paths(work)
        # resolve the config here too, so the render step uses the same one as the analysis
        meta = {"video": str(video), "config": d.get("config") or find_config(work, video.parent)}
        save_json(meta, paths["dir"] / "studio.json")
        remember(str(work), str(video))
        argv = [PY, "pipeline.py", "--video", str(video), "--workdir", str(work)]
        if meta["config"]:
            argv += ["--config", meta["config"]]
        for key, flag in (("url", "--url"), ("chat_file", "--chat-file"), ("start", "--start"), ("end", "--end")):
            if d.get(key):
                argv += [flag, str(d[key])]
        if d.get("auto_select"):
            argv.append("--auto-select")
        if d.get("force"):
            argv.append("--force")
        if not JOB.start("analyze", str(work), [(None, argv)]):
            return self.send_json({"error": "已有工作在執行中"}, 409)
        self.send_json({"ok": True, "work": str(work)})

    def render(self, d):
        work = d["work"]
        paths = work_paths(work)
        meta = studio_meta(paths)
        sel = d["selections"]
        # the source comes from the project, never from the request
        previous = load_json(paths["selections"]).get("source") if paths["selections"].exists() else None
        sel["source"] = meta.get("video") or previous
        if not sel["source"]:
            return self.send_json({"error": "這個專案沒有記錄直播檔路徑"}, 400)
        if not sel["clips"]:
            return self.send_json({"error": "沒有勾選任何片段"}, 400)
        save_json(sel, paths["selections"])
        cfg = ["--config", meta["config"]] if meta.get("config") else []
        cmds = []
        if d.get("refine"):
            cmds.append(("高品質重轉字幕", [PY, "refine_transcript.py", "--workdir", work, *cfg]))
        cmds += [("輸出影片與字幕", [PY, "cut_clips.py", "--workdir", work, *cfg]),
                 ("Premiere XML", [PY, "export_premiere_xml.py", "--workdir", work])]
        if not JOB.start("render", work, cmds):
            return self.send_json({"error": "已有工作在執行中"}, 409)
        self.send_json({"ok": True})

    def preview(self, d):
        meta = studio_meta(work_paths(d["work"]))
        if not meta.get("video"):
            return self.send_json({"error": "這個專案沒有記錄直播檔路徑"}, 400)
        ranges = [(float(a), float(b)) for a, b in d["ranges"] if float(b) > float(a)]
        if not ranges:
            return self.send_json({"error": "進出點不正確"}, 400)
        out = make_preview(d["work"], meta["video"], ranges)
        self.send_json({"url": "/file?path=" + quote(out)})

    def review_state(self, d):
        if not isinstance(d.get("clips"), list):
            return self.send_json({"error": "clips 格式不正確"}, 400)
        save_json({"clips": d["clips"]}, work_paths(d["work"])["dir"] / REVIEW_STATE)
        self.send_json({"ok": True})

    def pick(self, d):
        self.send_json({"path": pick_path(d.get("kind", "file"))})

    def open_path(self, d):
        if not allowed_file(d.get("path", "")):
            return self.send_json({"error": "不允許的檔案"}, 403)
        os.startfile(str(Path(d["path"]).resolve().parent))  # folders only; Windows only, like the rest of the workflow
        self.send_json({"ok": True})

    def send_file(self, p):
        size = p.stat().st_size
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        a, b = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):  # video seeking needs Range support
            if rng.group(1):
                a = int(rng.group(1))
                b = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                a = max(0, size - int(rng.group(2)))
            b = min(b, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(a)
            left = b - a + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Clip Review 已啟動：{url}（關閉這個視窗即停止）")
    if not args.no_browser:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
