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
import signal
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
from common import (atomic_output, default_workdir, find_config, fmt_ts, load_best_transcript, load_config, load_json,  # noqa: E402
                    parse_ts, probe, save_json, work_paths)
from cut_clips import clip_stem, resolve_template, vertical_filter  # noqa: E402
from learn_feedback import record as record_feedback  # noqa: E402
from tighten import audio_reader, load_energy, tighten_clip  # noqa: E402
from validate_selections import snap_clip, validate  # noqa: E402

PY = sys.executable
SETTINGS = Path.home() / ".clip-review.json"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
REVIEW_STATE = "review_state.json"  # every clip on the review page, checked or not, with unsaved edits
PROJECT_MARKERS = ("candidates.json", "studio.json", "selections.json")


SETTINGS_LOCK = threading.RLock()  # request threads and the job thread all read-modify-write the file


def settings():
    with SETTINGS_LOCK:
        if not SETTINGS.exists():
            return {"recent": []}
        try:
            return load_json(SETTINGS)
        except (ValueError, OSError) as e:
            # keep the broken file for the user instead of quietly overwriting it with an empty list
            bad = SETTINGS.with_name(SETTINGS.name + ".bad")
            try:
                os.replace(SETTINGS, bad)
            except OSError:
                pass
            print(f"[提示] 讀不到 {SETTINGS}（{e}），已改名成 {bad.name}；最近的專案清單從空的開始")
            return {"recent": []}


def update_settings(change):
    """Read, change and save the settings file as one step."""
    with SETTINGS_LOCK:
        s = settings()
        change(s)
        save_json(s, SETTINGS)


def remember(work, video):
    def change(s):
        rec = [r for r in s.get("recent", []) if r["work"] != work]
        s["recent"] = [{"work": work, "video": video}] + rec[:9]
    update_settings(change)


# ---------------------------------------------------------------- background job

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.kind, self.running, self.rc = None, False, None
        self.events, self.log, self.work = [], [], None
        self.proc, self.stopping, self.interrupted = None, False, False

    def snapshot(self):
        with self.lock:
            return {"kind": self.kind, "running": self.running, "rc": self.rc, "work": self.work,
                    "interrupted": self.interrupted, "events": self.events[-50:], "log": self.log[-80:]}

    def persist(self):
        """Keep the last job in the settings file, so a restart can report one that never finished."""
        snap = self.snapshot()
        update_settings(lambda s: s.__setitem__("last_job", snap))

    def restore(self):
        last = settings().get("last_job")
        if last and last.get("running"):
            self.kind, self.work, self.events, self.log = last["kind"], last["work"], last["events"], last["log"]
            self.rc, self.interrupted = -2, True

    def stop(self):
        """Stop the running job and everything it started (ffmpeg, whisper, claude...)."""
        with self.lock:
            if not self.running or not self.proc:
                return False
            self.stopping = True
            pid = self.proc.pid
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.killpg(pid, signal.SIGKILL)
        return True

    def start(self, kind, work, cmds):
        """cmds: list of (label, argv) or (label, argv, fallback). Run in order and stop at the first
        failure. fallback: a list of argv to try next; when all fail, the job goes on with a warning."""
        with self.lock:
            if self.running:
                return False
            self.kind, self.running, self.rc, self.work = kind, True, None, work
            self.events, self.log, self.stopping, self.interrupted = [], [], False, False
        self.persist()
        threading.Thread(target=self._run, args=(cmds,), daemon=True).start()
        return True

    def _run(self, cmds):
        rc = 0
        for cmd in cmds:
            label, argv = cmd[0], cmd[1]
            fallbacks = cmd[2] if len(cmd) > 2 else None
            if label:
                self._event({"step": label, "status": "start"})
            rc = self._exec(argv)
            for alt in fallbacks or []:
                if rc == 0 or self.stopping:
                    break
                with self.lock:
                    self.log.append(f"[{label}] 失敗，改用備用方式重試")
                rc = self._exec(alt)
            if rc != 0 and fallbacks is not None and not self.stopping:
                self._event({"step": label, "status": "fail", "reason": "沒有成功，沿用原本的逐字稿繼續；字幕可能較不準"})
                rc = 0
                continue
            if label:
                self._event({"step": label, "status": "done" if rc == 0 else "fail"})
            if rc != 0:
                break
        with self.lock:
            self.running, self.proc = False, None
            self.rc = -1 if self.stopping else rc
        self.persist()

    def _exec(self, argv):
        p = subprocess.Popen(argv, cwd=SCRIPTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=ENV,
                             start_new_session=os.name != "nt")  # own process group, so stop() reaches children
        with self.lock:
            self.proc = p
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
        return p.wait()

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
    wanted = {clip_stem(c) for c in (out["selections"] or {}).get("clips", [])}
    out["stale_clips"] = [Path(f).name for f in out["clips"] if out["selections"] and Path(f).stem not in wanted]
    return out


TRANSCRIPTS = {}  # work folder -> (modified times, sentences), since these files can be several MB


def transcript_for(paths):
    files = (paths["transcript_json"], paths["refined_json"])
    stamp = tuple(f.stat().st_mtime if f.exists() else 0 for f in files)
    key = str(paths["dir"])
    if TRANSCRIPTS.get(key, (None,))[0] != stamp:
        TRANSCRIPTS[key] = (stamp, load_best_transcript(paths))
    return TRANSCRIPTS[key][1]


def video_duration(paths, meta):
    """Length of the project's stream, or None when it can't be read."""
    try:
        return probe(project_source(paths, meta))["duration"]
    except (TypeError, RuntimeError, SystemExit):
        return None


def project_source(paths, meta):
    """The stream comes from the project, never from a request."""
    previous = load_json(paths["selections"]).get("source") if paths["selections"].exists() else None
    return meta.get("video") or previous


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
                "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]
        with atomic_output(out) as tmp:  # an interrupted render must not become the cached preview
            subprocess.run(cmd + [str(tmp)], check=True, capture_output=True)
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


def open_folder(path):
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], check=True)


# ---------------------------------------------------------------- layout calibration

def clean_region(r):
    """[x, y, w, h] of whole pixels, or None when it isn't a usable rectangle."""
    if not (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)):
        return None
    x, y, w, h = (int(round(v)) for v in r)
    return [x, y, w, h] if x >= 0 and y >= 0 and w >= 2 and h >= 2 else None


def clean_crop(c):
    """A crop preset from a request: [x, y, w, h], or {"region": ..., "pip": {"region": ..., "at": [x, y, w]}}."""
    if isinstance(c, list):
        return clean_region(c)
    if not isinstance(c, dict) or not clean_region(c.get("region")):
        return None
    out = {"region": clean_region(c["region"])}
    pip = c.get("pip")
    if pip:
        at = pip.get("at")
        if not (clean_region(pip.get("region")) and isinstance(at, list) and len(at) == 3
                and all(isinstance(v, (int, float)) and v >= 0 for v in at)):
            return None
        out["pip"] = {"region": clean_region(pip["region"]), "at": [int(v) for v in at]}
    return out


def project_config(paths, meta):
    """Config file of a project: the recorded one, one found nearby, or None."""
    src = project_source(paths, meta)
    return meta.get("config") or find_config(paths["dir"], *([Path(src).parent] if src else []))


def save_crop(config_path, name, crop):
    """Change one preset in the user's own config file; everything else in it stays."""
    raw = load_json(config_path) if Path(config_path).exists() else {}
    raw.setdefault("vertical_layout", {}).setdefault("crop_presets", {})[name] = crop
    save_json(raw, config_path)


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
                       "/api/open": self.open_path, "/api/review_state": self.review_state,
                       "/api/validate": self.validate, "/api/snap": self.snap, "/api/stop": self.stop,
                       "/api/tighten": self.tighten,
                       "/api/crop_state": self.crop_state, "/api/crop_frame": self.crop_frame,
                       "/api/crop_preview": self.crop_preview, "/api/crop_save": self.crop_save}.get(u.path)
            if not handler:
                return self.send_json({"error": "not found"}, 404)
            if ((u.path in ("/api/render", "/api/preview", "/api/review_state", "/api/validate", "/api/snap", "/api/tighten")
                 or u.path.startswith("/api/crop_")) and not known_project(data.get("work", ""))):
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
        if JOB.snapshot()["running"]:  # before touching studio.json: a refused request must not change the project
            return self.send_json({"error": "已有工作在執行中"}, 409)
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
        if d.get("visual"):
            argv.append("--visual")
        if not JOB.start("analyze", str(work), [(None, argv)]):
            return self.send_json({"error": "已有工作在執行中"}, 409)
        self.send_json({"ok": True, "work": str(work)})

    def render(self, d):
        work = d["work"]
        paths = work_paths(work)
        meta = studio_meta(paths)
        sel = d["selections"]
        sel["source"] = project_source(paths, meta)
        if not sel["source"]:
            return self.send_json({"error": "這個專案沒有記錄直播檔路徑"}, 400)
        if not sel["clips"]:
            return self.send_json({"error": "沒有勾選任何片段"}, 400)
        only = [str(x) for x in d.get("only") or []]
        cfg_data = load_config(meta.get("config"))
        blocked = [i for i in validate(sel["clips"], cfg_data, video_duration(paths, meta))
                   if i["level"] == "error" and (not only or i["id"] in only)]
        if blocked:
            return self.send_json({"error": "；".join(f"{i['id']}：{i['msg']}" for i in blocked)}, 400)
        save_json(sel, paths["selections"])
        try:
            record_feedback(work)
        except (KeyError, TypeError, ValueError, OSError) as e:  # learning must never block a render
            print(f"[提示] 沒有記錄到修改紀錄：{e}")
        cfg = ["--config", meta["config"]] if meta.get("config") else []
        pick = ["--only", *only] if only else []
        cmds = []
        if d.get("refine"):
            refine = [PY, "refine_transcript.py", "--workdir", work, *cfg, *pick]
            cmds.append(("高品質重轉字幕", refine, [[*refine, "--no-separate"]]))
        cmds += [("輸出影片與字幕", [PY, "cut_clips.py", "--workdir", work, *cfg, *pick]),
                 ("Premiere XML", [PY, "export_premiere_xml.py", "--workdir", work, *cfg])]
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

    def validate(self, d):
        paths = work_paths(d["work"])
        meta = studio_meta(paths)
        issues = validate(d["clips"], load_config(meta.get("config")), video_duration(paths, meta),
                          transcript_for(paths))
        self.send_json({"issues": issues})

    def snap(self, d):
        paths = work_paths(d["work"])
        meta = studio_meta(paths)
        self.send_json({"segments": snap_clip(d["clip"], transcript_for(paths), video_duration(paths, meta))})

    def tighten(self, d):
        paths = work_paths(d["work"])
        meta = studio_meta(paths)
        segs, notes, removed = tighten_clip(d["clip"], transcript_for(paths), load_energy(paths["audio"]),
                                            load_config(meta.get("config")),
                                            audio_reader(paths, project_source(paths, meta)))
        self.send_json({"segments": segs, "notes": notes, "removed": round(removed, 1)})

    def stop(self, d):
        self.send_json({"ok": JOB.stop()})

    def review_state(self, d):
        if not isinstance(d.get("clips"), list):
            return self.send_json({"error": "clips 格式不正確"}, 400)
        save_json({"clips": d["clips"]}, work_paths(d["work"])["dir"] / REVIEW_STATE)
        self.send_json({"ok": True})

    def crop_context(self, d):
        """(paths, stream path, stream info, vertical layout) of a project; the stream never comes from a request."""
        paths = work_paths(d["work"])
        meta = studio_meta(paths)
        src = project_source(paths, meta)
        if not src or not Path(src).is_file():
            raise ValueError("找不到這個專案的直播檔")
        return paths, src, probe(src), load_config(project_config(paths, meta))["vertical_layout"]

    def crop_state(self, d):
        paths, _, info, lay = self.crop_context(d)
        cands = load_json(paths["candidates"])["candidates"] if paths["candidates"].exists() else []
        self.send_json({"width": info["width"], "height": info["height"], "duration": info["duration"],
                        "presets": lay.get("crop_presets", {}), "content_box": lay["content_box"],
                        "template_mode": lay.get("mode", "template") == "template",
                        "has_template": bool(resolve_template(lay)),
                        "config": project_config(paths, studio_meta(paths)),
                        "at": cands[0]["peak"] if cands else min(60, info["duration"] / 2)})

    def crop_frame(self, d):
        paths, src, info, _ = self.crop_context(d)
        at = min(max(0.0, parse_ts(d["at"])), max(0.0, info["duration"] - 1))
        out = paths["dir"] / "previews" / "crop_frame.jpg"
        out.parent.mkdir(exist_ok=True)
        with atomic_output(out) as tmp:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", src, "-frames:v", "1",
                            "-q:v", "3", str(tmp)], check=True, capture_output=True)
        self.send_json({"url": "/file?path=" + quote(str(out))})

    def crop_preview(self, d):
        crop = clean_crop(d.get("crop"))
        if crop is None:
            return self.send_json({"error": "裁切範圍不正確"}, 400)
        paths, src, info, lay = self.crop_context(d)
        at = min(max(0.0, parse_ts(d["at"])), max(0.0, info["duration"] - 1))
        tpl = resolve_template(lay)
        out = paths["dir"] / "previews" / "crop_preview.jpg"
        out.parent.mkdir(exist_ok=True)
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", src]
        if tpl:
            cmd += ["-loop", "1", "-i", str(tpl)]
        cmd += ["-filter_complex", vertical_filter(lay, info["width"], info["height"], crop, bool(tpl)),
                "-map", "[out]", "-frames:v", "1", "-q:v", "3"]
        with atomic_output(out) as tmp:
            subprocess.run(cmd + [str(tmp)], check=True, capture_output=True)
        self.send_json({"url": "/file?path=" + quote(str(out))})

    def crop_save(self, d):
        name, crop = str(d.get("name", "")).strip(), clean_crop(d.get("crop"))
        if not re.fullmatch(r"[\w-]{1,30}", name) or crop is None:
            return self.send_json({"error": "名稱或裁切範圍不正確（名稱只能有文字、數字、底線、連字號）"}, 400)
        paths, src, _, _ = self.crop_context(d)
        meta = studio_meta(paths)
        config = project_config(paths, meta) or str(Path(src).parent / "config.json")
        save_crop(config, name, crop)
        if meta.get("config") != config:
            save_json({**meta, "video": project_source(paths, meta), "config": config}, paths["dir"] / "studio.json")
        self.send_json({"ok": True, "config": config})

    def pick(self, d):
        self.send_json({"path": pick_path(d.get("kind", "file"))})

    def open_path(self, d):
        if not allowed_file(d.get("path", "")):
            return self.send_json({"error": "不允許的檔案"}, 403)
        open_folder(str(Path(d["path"]).resolve().parent))  # folders only
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
    JOB.restore()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Clip Review 已啟動：{url}（關閉這個視窗即停止）")
    if not args.no_browser:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
