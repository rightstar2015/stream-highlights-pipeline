"""End-to-end smoke test with a synthetic 5-minute stream. Run after every change:

    python tests/smoke_test.py            (keeps output in tests/_out for inspection)

Needs only ffmpeg + numpy (no whisper, no network). Checks:
  chat parsing, audio energy, signal merge (top candidate must land on the staged event),
  template vertical render (1080x1920), multi-segment horizontal render, SRT, loudness,
  Premiere XML structure + markers, reference analysis.
"""
import json
import shutil
import time
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

# Windows consoles default to cp950; force UTF-8 so Chinese labels print safely.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
OUT = HERE / "_out"
W = OUT / "work"
PY = sys.executable


def sh(*args):
    r = subprocess.run([PY, *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=SCRIPTS)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"FAIL: {' '.join(map(str, args[:2]))}")
    return r.stdout


def make_fixture():
    shutil.rmtree(OUT, ignore_errors=True)
    W.mkdir(parents=True)
    video = OUT / "stream.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=300",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=300:sample_rate=48000",
                    "-f", "lavfi", "-i", "sine=frequency=2500:duration=300:sample_rate=48000",
                    "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=300:sample_rate=48000",
                    "-filter_complex",
                    # white flash at 150s and fade to black at 250s over the game area
                    "[0:v]drawbox=x=1440:y=560:w=480:h=520:color=pink@0.9:t=fill,"
                    "drawbox=x=420:y=0:w=1000:h=1080:color=white:t=fill:enable='between(t,150,151)',"
                    "drawbox=x=420:y=0:w=1000:h=1080:color=black:t=fill:enable='between(t,250,254)'[v];"
                    "[1:a]volume='if(between(t,100,104)+between(t,200,203),1.0,0.05)':eval=frame[a0];"
                    # a high-pitched yell at 160s and 5 Hz pulsing "laughter" at 260s
                    "[2:a]volume='if(between(t,160,162),0.8,0)':eval=frame[s];"
                    "[3:a]tremolo=f=5:d=1,volume='if(between(t,260,264),0.8,0)':eval=frame[l];"
                    "[a0][s][l]amix=inputs=3:normalize=0,aformat=channel_layouts=stereo[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
                    "-c:a", "aac", str(video)], check=True)
    # chat replay in yt-dlp live_chat.json format
    lines = []

    def msg(t, text, sc=None):
        key = "liveChatPaidMessageRenderer" if sc else "liveChatTextMessageRenderer"
        r = {"authorName": {"simpleText": "u"}, "message": {"runs": [{"text": text}]}}
        if sc:
            r["purchaseAmountText"] = {"simpleText": sc}
        return json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {key: r}}}],
                                                    "videoOffsetTimeMsec": str(int(t * 1000))}}, ensure_ascii=False)
    for t in range(0, 300, 4):
        lines.append(msg(t + 0.3, "嗨嗨"))
    for i in range(60):
        lines.append(msg(105 + i * 0.15, ["笑死", "草", "？？？", "8787"][i % 4]))
    lines.append(msg(108, "笑死 給你買藥", "NT$150"))
    (OUT / "chat.live_chat.json").write_text("\n".join(lines), encoding="utf-8")
    # transcript as transcribe.py would write it
    texts = {98: "好我們來打這隻王，", 101: "欸欸欸等一下等一下！", 103.5: "啊啊啊不要不要",
             106: "我死了。怎麼可能", 109: "你們不要笑啦"}
    for k in range(0, 300, 3):
        if all(abs(k - x) >= 3 for x in texts):
            texts[k] = "然後我們往這邊走"
    segs = []
    for t in sorted(texts):
        tx = texts[t]
        words = [{"start": t + i * 0.22, "end": t + i * 0.22 + 0.2, "word": ch} for i, ch in enumerate(tx)]
        segs.append({"start": t, "end": words[-1]["end"], "text": tx, "words": words})
    (W / "transcript.json").write_text(json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    return video


def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", str(p)], capture_output=True, text=True)
    d = json.loads(r.stdout)
    v = [s for s in d["streams"] if "width" in s][0]
    return v["width"], v["height"], float(d["format"]["duration"])


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


def main():
    print("建立測試素材...")
    video = make_fixture()
    sh("fetch_chat.py", "--file", OUT / "chat.live_chat.json", "--workdir", W)
    sh("audio_energy.py", "--video", video, "--workdir", W)
    sh("visual_events.py", "--video", video, "--workdir", W)
    sh("vocal_events.py", "--video", video, "--workdir", W)
    vis = np.genfromtxt(W / "visual_events.csv", delimiter=",", names=True)
    voc = np.genfromtxt(W / "vocal_events.csv", delimiter=",", names=True)
    check(149 <= vis["t"][vis["flash"].argmax()] <= 151, f"畫面偵測：白閃在 150s（{vis['t'][vis['flash'].argmax()]:.0f}s）")
    check(249 <= vis["t"][vis["fade"].argmax()] <= 251, f"畫面偵測：轉黑在 250s（{vis['t'][vis['fade'].argmax()]:.0f}s）")
    check(159 <= voc["t"][voc["scream"].argmax()] <= 163, f"人聲偵測：尖叫在 160s（{voc['t'][voc['scream'].argmax()]:.0f}s）")
    check(259 <= voc["t"][voc["laugh"].argmax()] <= 265, f"人聲偵測：笑聲在 260s（{voc['t'][voc['laugh'].argmax()]:.0f}s）")
    # new signals are weight 0 by default; enable them here to test the merge
    (OUT / "cfg_signals.json").write_text(json.dumps(
        {"signals": {"weights": {"visual": 0.6, "scream": 0.8, "laugh": 0.8}}}), encoding="utf-8")
    sh("signals.py", "--workdir", W, "--config", OUT / "cfg_signals.json")
    cands = json.loads((W / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    top = min(cands, key=lambda c: c["rank"])
    check(95 <= top["peak"] <= 112, f"第一名候選落在事件附近（peak={top['peak']}s）")
    # events 10 s apart merge into one candidate (min_gap_sec), so check each candidate's breakdown
    def flagged(sig, at):
        return any(c["peak"] - 15 <= at <= c["peak"] + 10 and c["breakdown"][sig] > 2 for c in cands)
    check(all(flagged(s, at) for s, at in (("visual", 150), ("scream", 160), ("visual", 250), ("laugh", 260))),
          f"畫面／人聲事件都出現在候選的訊號裡（{[(c['peak'], c['breakdown']) for c in cands]}）")

    # pipeline: a pre-made transcript must be skipped (no whisper needed); everything else runs
    W2 = OUT / "pipe"
    W2.mkdir()
    shutil.copy(W / "transcript.json", W2 / "transcript.json")
    out = sh("pipeline.py", "--video", video, "--workdir", W2, "--chat-file", OUT / "chat.live_chat.json",
             "--no-separate", "--frames", "3", "--visual")
    ev = [json.loads(l[3:]) for l in out.splitlines() if l.startswith("@@ ")]
    st = {e["step"]: e["status"] for e in ev}
    check(st.get("transcribe") == "skip" and all(st.get(s) == "done" for s in
          ("chat", "audio", "visual", "vocal", "signals", "frames")) and not ev[-1]["failed"],
          f"pipeline：已有逐字稿就跳過，其餘步驟完成（{st}）")
    check(len(list((W2 / "frames").glob("*.jpg"))) == 3, "pipeline：候選截圖 3 張")
    out = sh("pipeline.py", "--video", video, "--workdir", W2, "--no-separate", "--frames", "3")
    st = {json.loads(l[3:])["step"]: json.loads(l[3:])["status"] for l in out.splitlines() if l.startswith("@@ ")}
    check(all(st.get(s) == "skip" for s in ("transcribe", "audio", "visual", "vocal")) and st["signals"] == "done",
          "pipeline：重跑時沿用已完成的步驟")
    r = subprocess.run([PY, "pipeline.py", "--video", str(video), "--workdir", str(W2), "--no-separate", "--start", "0:10"],
                       cwd=SCRIPTS, capture_output=True, text=True, encoding="utf-8", errors="replace")
    check(r.returncode == 2 and "可能過期" in r.stdout, "pipeline：分析範圍改變時提醒，不沿用舊結果")

    W3 = OUT / "range"
    W3.mkdir()
    shutil.copy(W / "transcript.json", W3 / "transcript.json")
    out = sh("pipeline.py", "--video", video, "--workdir", W3, "--no-separate", "--frames", "1",
             "--start", "1:00", "--end", "1:20")
    st = {json.loads(l[3:])["step"]: json.loads(l[3:]) for l in out.splitlines() if l.startswith("@@ ")}
    rows = np.genfromtxt(W3 / "audio_energy.csv", delimiter=",", names=True)
    check(st["visual"]["status"] == "skip" and not (W3 / "visual_events.csv").exists(), "pipeline：預設跳過畫面分析")
    check(rows["t"][0] == 60 and len(rows) == 20, "音量分析遵守 --start/--end")

    # Clip Review server: project state + byte ranges (video seeking), on a throwaway port/settings file
    sys.path.insert(0, str(HERE.parent / "app"))
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    import server as studio
    studio.SETTINGS = OUT / "studio_settings.json"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), studio.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from urllib.parse import quote
    st = json.loads(urllib.request.urlopen(f"{base}/api/project?work={quote(str(W2))}").read())
    check(len(st["candidates"]) > 0 and any(c["frame"] for c in st["candidates"]),
          f"Clip Review：讀得到專案候選與截圖（{len(st['candidates'])} 個）")
    jpg = next((W2 / "frames").glob("*.jpg"))
    req = urllib.request.Request(f"{base}/file?path={quote(str(jpg))}", headers={"Range": "bytes=10-19"})
    r = urllib.request.urlopen(req)
    check(r.status == 206 and r.read() == jpg.read_bytes()[10:20], "Clip Review：檔案支援 Range（影片可拖曳）")
    try:
        urllib.request.urlopen(f"{base}/file?path={quote(str(video))}")
        blocked = False
    except urllib.error.HTTPError as e:
        blocked = e.code == 403
    check(blocked, "Clip Review：不提供專案資料夾以外的檔案")

    def status(path, body=None, **headers):
        data = None if body is None else json.dumps(body).encode("utf-8")
        hdr = {k.replace("_", "-"): v for k, v in headers.items()}
        if body is not None:
            hdr.setdefault("Content-Type", "application/json")
        try:
            return urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=hdr)).status
        except urllib.error.HTTPError as e:
            return e.code
    state = {"work": str(W2), "clips": [{"id": "v01", "on": False, "title": "我改過的"}]}
    check(status("/api/job", Host="evil.example") == 403, "Clip Review：擋下其他 Host（DNS rebinding）")
    check(status("/api/review_state", state, Origin="https://evil.example") == 403, "Clip Review：擋下其他網站的 POST")
    check(status("/api/review_state", state, Content_Type="text/plain") == 415, "Clip Review：POST 必須是 JSON")
    check(status("/api/job", Sec_Fetch_Site="cross-site") == 403, "Clip Review：擋下其他網站的 GET")
    check(status("/api/review_state", dict(state, work=str(OUT))) == 403, "Clip Review：不接受未開啟的資料夾")
    check(status(f"/api/project?work={quote(str(OUT / 'ref_none'))}") == 404
          and status(f"/api/project?work={quote(str(HERE))}") == 400, "Clip Review：一般資料夾不會被當成專案")
    check(status("/api/open", {"path": str(video)}) == 403, "Clip Review：不開啟專案以外的路徑")
    check(status("/api/review_state", state, Origin=base) == 200, "Clip Review：接受自己頁面的請求")
    st = json.loads(urllib.request.urlopen(f"{base}/api/project?work={quote(str(W2))}").read())
    check(st["review_state"]["clips"][0]["title"] == "我改過的", "Clip Review：審核內容重新開啟後還在")

    # background job: stop kills the whole process tree, refine-style steps fall back and carry on
    slow = [PY, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"]
    fail = [PY, "-c", "raise SystemExit(3)"]
    def wait_job(seconds=10):
        for _ in range(seconds * 10):
            if not studio.JOB.snapshot()["running"]:
                return studio.JOB.snapshot()
            time.sleep(0.1)
        raise SystemExit("FAIL: 工作沒有結束")
    check(studio.JOB.start("render", str(W2), [("慢", slow)]), "工作：可以啟動")
    time.sleep(1)
    check(status("/api/stop", {}) == 200 and wait_job()["rc"] == -1, "工作：可以停止")
    tg = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/tighten", data=json.dumps(
        {"work": str(W2), "clip": {"id": "v", "format": "vertical", "segments": [{"in": 95, "out": 115}]}}).encode(),
        headers={"Content-Type": "application/json"})).read())
    check(tg["removed"] > 0 and len(tg["segments"]) > 1, f"Clip Review：剪掉停頓（{tg['removed']} 秒、{len(tg['segments'])} 段）")
    check(status("/api/tighten", {"work": str(OUT), "clip": {}}) == 403, "Clip Review：剪掉停頓只接受已開啟的專案")
    studio.JOB.start("render", str(W2), [("重轉", fail, [fail]), ("出片", [PY, "-c", "print('ok')"])])
    j = wait_job()
    check(j["rc"] == 0 and any(e["step"] == "出片" and e["status"] == "done" for e in j["events"])
          and any(e["step"] == "重轉" and e.get("reason") for e in j["events"]), "工作：重轉失敗會提醒，但仍繼續出片")
    studio.JOB.start("render", str(W2), [("失敗", fail)])
    check(wait_job()["rc"] == 3, "工作：一般步驟失敗就停止")
    studio.save_json({"recent": studio.settings()["recent"],
                      "last_job": {"running": True, "kind": "analyze", "work": str(W2), "events": [], "log": []}}, studio.SETTINGS)
    studio.JOB.restore()
    check(studio.JOB.snapshot()["interrupted"], "工作：重開伺服器後會顯示上次沒做完")

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    # analyze -> preview -> render through the API, as the page does it
    W4 = OUT / "api"
    W4.mkdir()
    shutil.copy(W / "transcript.json", W4 / "transcript.json")
    api_cfg = OUT / "api_config.json"
    api_cfg.write_text(json.dumps({"streamer_name": "測試主播", "text_replacements": {"甲": "乙"}}, ensure_ascii=False), encoding="utf-8")
    check(post("/api/analyze", {"video": str(video), "work": str(W4), "config": str(api_cfg),
                                "chat_file": str(OUT / "chat.live_chat.json")})["ok"], "API 分析：可以啟動")
    check(status("/api/analyze", {"video": str(video), "work": str(W4)}) == 409
          and json.loads((W4 / "studio.json").read_text(encoding="utf-8"))["config"] == str(api_cfg),
          "API 分析：進行中會拒絕，也不會改掉專案設定")
    j = wait_job(180)
    check(j["rc"] == 0 and (W4 / "candidates.json").exists(), f"API 分析：跑完產生候選（{j['rc']}）")
    url = post("/api/preview", {"work": str(W4), "ranges": [[10, 12], [20, 21]]})["url"]
    pv = urllib.request.urlopen(base + url).read()
    pv_file = OUT / "pv.mp4"
    pv_file.write_bytes(pv)
    check(abs(probe(pv_file)[2] - 3) < 0.5, "API 預覽：兩段接起來約 3 秒")
    clip = {"id": "v09", "format": "vertical", "title": "API測試", "segments": [{"in": 100, "out": 108, "crop": "game"}]}
    check(status("/api/render", {"work": str(W4), "selections": {"clips": []}}) == 400, "API 出片：沒勾選會擋下")
    check(status("/api/render", {"work": str(W4), "selections": {"clips": [
        dict(clip, segments=[{"in": 100, "out": 9999}])]}}) == 400, "API 出片：超出直播長度會擋下")
    check(post("/api/render", {"work": str(W4), "selections": {"clips": [clip]}})["ok"], "API 出片：可以啟動")
    j = wait_job(120)
    check(j["rc"] == 0 and (W4 / "clips" / "v09_API測試.mp4").exists() and list(W4.glob("*_premiere.xml")),
          f"API 出片：產生 MP4 和 XML（{j['rc']}）")

    # layout calibration
    check(status("/api/crop_state", {"work": str(OUT)}) == 403, "版型校正：不接受未開啟的資料夾")
    cs = post("/api/crop_state", {"work": str(W4)})
    check(cs["width"] == 1920 and "game" in cs["presets"] and cs["config"] == str(api_cfg), "版型校正：讀得到直播尺寸和裁切")
    frame = urllib.request.urlopen(base + post("/api/crop_frame", {"work": str(W4), "at": "1:40"})["url"]).read()
    check(frame[:2] == b"\xff\xd8", "版型校正：可以取得畫面")
    res = OUT / "crop_preview.jpg"
    res.write_bytes(urllib.request.urlopen(base + post("/api/crop_preview", {
        "work": str(W4), "at": 100, "crop": [420, 0, 1000, 1080]})["url"]).read())
    check(probe(res)[:2] == (1080, 1920), "版型校正：預覽是 1080x1920")
    check(status("/api/crop_preview", {"work": str(W4), "at": 100, "crop": [0, 0, 0, 0]}) == 400, "版型校正：不接受空的範圍")
    check(status("/api/crop_save", {"work": str(W4), "name": "../x", "crop": [0, 0, 10, 10]}) == 400, "版型校正：不接受奇怪的名稱")
    post("/api/crop_save", {"work": str(W4), "name": "game", "crop": [400, 10, 1000, 1080]})
    post("/api/crop_save", {"work": str(W4), "name": "game_pip", "crop": {
        "region": [1, 2, 300, 300], "pip": {"region": [5, 6, 100, 100], "at": [0, 0, 380]}}})
    raw = json.loads(api_cfg.read_text(encoding="utf-8"))
    merged = studio.load_config(api_cfg)["vertical_layout"]["crop_presets"]
    check(raw["streamer_name"] == "測試主播" and raw["text_replacements"] == {"甲": "乙"} and "output_size" not in raw["vertical_layout"],
          "版型校正：只改裁切，設定檔其他內容不動")
    check(merged["game"] == [400, 10, 1000, 1080] and merged["game_pip"]["pip"]["region"] == [5, 6, 100, 100]
          and "face" in merged, "版型校正：存檔後出片讀得到新範圍，其他裁切還在")
    srv.shutdown()

    # headless picks: Claude's printed result is validated before anything is written
    from pipeline import parse_auto_output

    def result(draft, review="# 候選"):
        body = draft if isinstance(draft, str) else json.dumps(draft, ensure_ascii=False)
        return (f"<<<CANDIDATES_REVIEW>>>\n{review}\n<<<END_CANDIDATES_REVIEW>>>\n"
                f"<<<SELECTIONS_DRAFT_JSON>>>\n{body}\n<<<END_SELECTIONS_DRAFT_JSON>>>")

    def rejected(text):
        try:
            parse_auto_output(text, "x.mp4")
            return False
        except ValueError:
            return True
    good = {"id": "v01", "format": "vertical", "title": "t", "confidence": "高", "layout": {"template_png": "x"},
            "segments": [{"in": "1:00", "out": "1:20", "crop": "game", "evil": 1}]}
    fake = result({"source": "other.mp4", "clips": [{"id": "x", "format": "horizontal", "segments": [[1, 2]]}]})
    review, draft = parse_auto_output(fake + "\n" + result({"source": "other.mp4", "clips": [good]}), "x.mp4")
    c = draft["clips"][0]
    check(review == "# 候選" and draft["source"] == "x.mp4" and c["id"] == "v01" and "layout" not in c
          and c["segments"] == [{"in": "1:00", "out": "1:20", "crop": "game"}],
          "自動挑選：只取最後一份結果，來源固定，移除多餘欄位")
    check(parse_auto_output(result("```json\n" + json.dumps({"clips": [good]}) + "\n```"), "x.mp4")[1]["clips"],
          "自動挑選：接受 JSON 程式碼區塊")
    check(all(rejected(t) for t in ("沒有標記", result("{壞掉"), result({"clips": []}),
                                    result({"clips": [dict(good, id="../x")]}),
                                    result({"clips": [dict(good, format="other")]}),
                                    result({"clips": [good, good]}),
                                    result({"clips": [dict(good, segments=[[20, 10]])]}))),
          "自動挑選：不合格的結果都不會存檔")

    # low-confidence transcript filter
    from types import SimpleNamespace as NS
    from transcribe import low_confidence
    lim = {"compression_ratio_max": 2.4, "no_speech_prob_max": 0.6, "avg_logprob_min": -1.0}
    seg_ok = NS(compression_ratio=1.5, no_speech_prob=0.9, avg_logprob=-0.3)
    check(low_confidence(seg_ok, lim) is None, "幻覺過濾：沒人說話機率高但信心夠，保留")
    check(low_confidence(NS(compression_ratio=3.0, no_speech_prob=0, avg_logprob=0), lim)
          and low_confidence(NS(compression_ratio=1, no_speech_prob=0.9, avg_logprob=-1.5), lim),
          "幻覺過濾：重複度過高、或沒人說話且信心低，都濾掉")
    check(low_confidence(NS(compression_ratio=9, no_speech_prob=1, avg_logprob=-9), None) is None, "幻覺過濾：可以關閉")

    # correction log
    from learn_feedback import compare, record
    clip = lambda i, a, b, **k: {"id": i, "format": "vertical", "title": i, "segments": [[a, b]], **k}
    res = compare([clip("a", 100, 130), clip("b", 200, 230), clip("c", 300, 330, title="被刪的", confidence="低")],
                  [clip("x", 103, 128, title="改名"), clip("b", 200, 230), clip("y", 900, 930)])
    check(len(res["kept"]) == 2 and [c["title"] for c in res["removed"]] == ["被刪的"] and [c["id"] for c in res["added"]] == ["y"]
          and any(k["in_shift"] == 3 and k["out_shift"] == -2 for k in res["kept"]), "修改紀錄：分得出保留、刪除、新增和剪點偏移")
    W4 = OUT / "fb" / "20260101_精華"
    W4.mkdir(parents=True)
    (W4 / "selections.draft.json").write_text(json.dumps({"clips": [clip("a", 100, 130)]}), encoding="utf-8")
    (W4 / "selections.json").write_text(json.dumps({"clips": [clip("a", 102, 130)]}), encoding="utf-8")
    flog = OUT / "fb" / "log.md"
    record(W4, flog)
    (W4 / "selections.json").write_text(json.dumps({"clips": []}), encoding="utf-8")
    record(W4, flog)
    text = flog.read_text(encoding="utf-8")
    check(text.count("## 20260101_精華") == 1 and "保留 0、刪除 1" in text, "修改紀錄：同一個專案重跑時更新，不重複")

    # term suggestions
    from suggest_terms import srt_text, suggest
    (OUT / "o.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n今天打小銘王\n", encoding="utf-8-sig")
    check(srt_text(OUT / "o.srt") == "今天打小銘王", "專有名詞：SRT 只取文字")
    pairs = [("今天打小銘王很難", "今天打小明王很難"), ("明天再打小銘王吧", "明天再打小明王吧"), ("我吃蘋果了", "我吃香蕉了")]
    check(suggest(pairs) == {"小銘": "小明"}, f"專有名詞：只建議重複出現的修改（{suggest(pairs)}）")

    # chat offset hint
    from signals import estimate_chat_shift, offset_hint
    rng = np.random.default_rng(1)
    loud_s = np.zeros(4000)
    bursts = rng.integers(0, 3900, 60)
    for b in bursts:
        loud_s[b:b + 15] = 10
    chat_s = np.zeros(4000)
    for b in bursts:
        if b + 70 < 4000:
            chat_s[b + 60:b + 70] += 5
    check(estimate_chat_shift(chat_s, loud_s)[0] in range(55, 66), f"時間差：估得出聊天室晚 60 秒（{estimate_chat_shift(chat_s, loud_s)}）")
    check("chat_offset_sec" in offset_hint([{"t": 1}], chat_s, loud_s, 0, 4000), "時間差：差很多時提示")
    check(offset_hint([{"t": 1}], np.roll(chat_s, -55), loud_s, 0, 4000) is None, "時間差：對齊時不提示")
    check("多了" in offset_hint([{"t": 4100}], chat_s * 0, loud_s, 0, 4000), "時間差：聊天室比影片長時提示")
    check(estimate_chat_shift(np.zeros(4000), loud_s) is None, "時間差：沒有留言就不猜")

    # evaluation tool
    sys.path.insert(0, str(HERE.parent / "tools"))
    from eval_selections import eval_candidates, eval_pred
    tr = [{"id": "t1", "segments": [[100, 120]]}, {"id": "t2", "segments": [[500, 520]]}]
    ev = eval_pred(tr, [{"id": "p", "segments": [[100, 110], [105, 120]]}, {"id": "q", "segments": [[800, 820]]}])
    check(ev["rows"][0]["coverage"] == 1 and ev["rows"][1]["coverage"] == 0 and ev["hit"] == 1 and ev["precision"] == 0.5,
          "評估工具：重疊不重複計算，涵蓋率和精準度正確")
    check(eval_candidates(tr, [{"peak": 105, "rank": 3}, {"peak": 900, "rank": 1}])[0]["best_rank"] == 3, "評估工具：候選名次正確")

    from validate_selections import snap_range, validate
    words = [{"start": 10 + i * 0.5, "end": 10.4 + i * 0.5, "word": "字"} for i in range(40)]
    sent = [{"start": 10.0, "end": 29.9, "text": "長句", "words": words}]
    check(np.allclose(snap_range(11.1, 29.0, sent), (9.8, 30.1)), "對齊句子：靠近句子邊界就吸過去")
    a, b = snap_range(20.2, 25.1, sent)
    check(abs(a - 19.95) < 0.01 and abs(b - 25.45) < 0.01, "對齊句子：離邊界遠時只避開切到字")
    check(snap_range(5, 40, sent) == (5, 40), "對齊句子：不在句子內就不動")
    vcfg = {"limits": {"vertical_sec": [18, 35]}, "vertical_layout": {"crop_presets": {"game": [0, 0, 1, 1]}}}
    base_clip = {"id": "v01", "format": "vertical", "segments": [{"in": 40, "out": 60, "crop": "game"}]}
    levels = lambda *c, **k: [(i["level"], i["msg"]) for i in validate(list(c), vcfg, **k)]
    check(levels(base_clip, duration=100) == [], "選片檢查：正常的片段沒有提醒")
    check(levels(base_clip, duration=50)[0][0] == "error", "選片檢查：超出直播長度是錯誤")
    check(levels(dict(base_clip, segments=[{"in": 60, "out": 40}]))[0][0] == "error", "選片檢查：進點大於出點是錯誤")
    check(levels(dict(base_clip, segments=[{"in": 40, "out": 60, "crop": "faec"}]))[0][0] == "error", "選片檢查：裁切不存在是錯誤")
    check(levels(base_clip, base_clip)[0][0] == "error", "選片檢查：id 重複是錯誤")
    check(levels(dict(base_clip, segments=[{"in": 0, "out": 50}]))[0][0] == "warning", "選片檢查：太長只是提醒")
    check(any("句子中間" in m for _, m in [(i["level"], i["msg"]) for i in
              validate([dict(base_clip, segments=[{"in": 15, "out": 40}])], vcfg, segs=sent)]), "選片檢查：切在句子中間會提醒")

    # speaker labels (the pyannote model itself is not run here)
    from diarize import clip_to_source, label_srt, speaker_for
    turns = [[10, 20, "A"], [20, 30, "B"], [40, 50, "A"], [40, 50, "B"]]
    check(speaker_for(11, 19, turns) == "A" and speaker_for(18, 29, turns) == "B" and speaker_for(60, 70, turns) is None,
          "說話者：取講最多的人，沒人說話就不猜")
    check(speaker_for(41, 49, [[41, 45, "A"], [45, 49, "B"], [45, 49, "C"]]) is None, "說話者：沒有人過半就不猜")
    jump = [{"a": 10, "b": 20}, {"a": 100, "b": 110}]
    check(clip_to_source(jump, 5) == 15 and clip_to_source(jump, 12) == 102, "說話者：跳剪的時間換算正確")
    srt = """1
00:00:01,000 --> 00:00:02,000
第一句

2
00:00:11,000 --> 00:00:12,000
第二句
"""
    got = label_srt(srt, [{"a": 10, "b": 20}, {"a": 20, "b": 30}], turns, {"A": "小明"})
    check("[小明] 第一句" in got and "[B] 第二句" in got and "00:00:11,000 --> 00:00:12,000" in got,
          f"說話者：SRT 加上標籤，時間不變（{got!r}）")

    from pipeline import limit_picks
    picks = [{"id": f"v{i}", "format": "vertical", "confidence": c} for i, c in enumerate(["低", "中", "高", "高"])]
    picks.append({"id": "h1", "format": "horizontal", "confidence": "低"})
    kept, dropped = limit_picks(picks, {"max_vertical": 2, "max_horizontal": 1})
    check([c["id"] for c in kept] == ["v2", "v3", "h1"] and {c["id"] for c in dropped} == {"v0", "v1"},
          "推薦上限：依信心留下較好的，直式橫式分開算")

    from pipeline import coverage_warning
    tm = """## 話題地圖
- [0:00:00–1:00:00] 開場
- [1:00:00–5:50:00] 雜談

## 候選
- [5:59:00–6:00:00] 不算地圖
"""
    check(coverage_warning(tm, 6 * 3600) is None, "話題地圖：涵蓋整場時不警告")
    check("只涵蓋" in coverage_warning(tm.replace("5:50:00", "2:00:00"), 6 * 3600), "話題地圖：沒讀到結尾會警告")
    gap = """## 話題地圖
- [0:00:00–1:00:00] a
- [3:00:00–6:00:00] b
"""
    check("空白" in coverage_warning(gap, 6 * 3600),
          "話題地圖：中間大段空白會警告")
    check("沒有話題地圖" in coverage_warning("# 候選", 6 * 3600), "話題地圖：沒有地圖會警告")

    sel = {"source": str(video), "clips": [
        {"id": "v01", "format": "vertical", "title": "打王暴斃", "header_title": "我破防了",
         "segments": [{"in": "1:37.7", "out": "1:44", "crop": "game_pip"},
                      {"in": "1:44", "out": "1:51", "crop": "face"}],
         "notes": [{"at": "1:43", "text": "插入驚嚇卡"}]},
        {"id": "h01", "format": "horizontal", "title": "打王全過程",
         "segments": [[90, 112], {"in": 197.5, "out": 205}],
         "notes": [{"at": "1:42", "text": "放大臉"}, {"at": "2:30", "text": "（被剪掉，不應出現）"}]}]}
    (W / "selections.json").write_text(json.dumps(sel, ensure_ascii=False), encoding="utf-8")
    sh("cut_clips.py", "--workdir", W, "--preview")
    sh("cut_clips.py", "--workdir", W)
    sh("export_premiere_xml.py", "--workdir", W)

    vw, vh, vd = probe(W / "clips" / "v01_打王暴斃.mp4")
    check((vw, vh) == (1080, 1920), "直式 1080x1920")
    check(abs(vd - 13.3) < 0.3, f"直式長度 13.3s（實際 {vd:.2f}）")
    hw, hh, hd = probe(W / "clips" / "h01_打王全過程.mp4")
    check((hw, hh) == (1920, 1080), "橫式維持原解析度")
    check(abs(hd - 29.5) < 0.3, f"橫式兩段跳剪 29.5s（實際 {hd:.2f}）")
    srt = (W / "clips" / "v01_打王暴斃.srt").read_text(encoding="utf-8-sig")
    check("欸欸欸等一下等一下" in srt and "。" not in srt, "SRT 內容正確且無句尾標點")

    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(W / "clips" / "v01_打王暴斃.mp4"), "-af", "ebur128",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    i_line = [l for l in r.stderr.splitlines() if l.strip().startswith("I:")][-1]
    lufs = float(i_line.split()[1])
    check(abs(lufs + 14) < 1.5, f"響度約 -14 LUFS（實際 {lufs}）")

    root = ET.parse(next(W.glob("*_premiere.xml"))).getroot()
    seqs = {s.find("name").text: s for s in root.iter("sequence")}
    check(len(seqs) == 2, "XML 兩個序列")
    h = [s for n, s in seqs.items() if n.startswith("h01")][0]
    items = h.findall("media/video/track/clipitem")
    check([i.find("in").text for i in items] == ["2700", "5925"], "橫式剪點格數正確（30fps）")
    names = [m.find("name").text for m in h.findall("marker")]
    check(names == ["字幕：匯入 h01_打王全過程.srt", "放大臉"], f"標記點正確且剪掉的段落不出現（{names}）")
    v = [s for n, s in seqs.items() if n.startswith("v01")][0]
    start = [m.find("name").text for m in v.findall("marker") if m.find("in").text == "0"]
    check(len(start) == 1 and "標題：我破防了" in start[0] and "字幕：匯入 v01_打王暴斃.srt" in start[0],
          "直式 00:00 標記含標題與要匯入的字幕檔")

    from export_premiere_xml import card_plan, place_region
    pl = place_region([420, 0, 1000, 1080], 1920, 1080, (0, 328, 1080, 1168))
    s = pl["scale"] / 100
    left, right = pl["crop"]["left"] / 100 * 1920, 1920 - pl["crop"]["right"] / 100 * 1920
    top, bottom = pl["crop"]["top"] / 100 * 1080, 1080 - pl["crop"]["bottom"] / 100 * 1080
    edges = [pl["cx"] + (left - 960) * s, pl["cx"] + (right - 960) * s,
             pl["cy"] + (top - 540) * s, pl["cy"] + (bottom - 540) * s]
    check(all(abs(a - b) < 0.01 for a, b in zip(edges, [0, 1080, 328, 1496])),
          f"可調整序列：裁切後剛好填滿內容視窗（{edges}）")
    over, end = card_plan([(3.0, "轉場字卡"), (None, "接片尾"), (None, "轉場字卡"), (4.0, "放大臉")],
                          [{"match": "轉場字卡", "place": "at"}, {"match": "接片尾", "place": "end"}], 10)
    check([t for t, _ in over] == [3.0] and end["match"] == "接片尾", "字卡：剪掉的轉場不放，片尾不受時間影響")

    # avatar_tracks: cards on V2 become avatars on V3, close cards merge, excluded ones drop
    export = ET.Element("xmeml", version="4")
    exq = ET.SubElement(export, "sequence")
    ET.SubElement(exq, "name").text = "匯出序列"
    rate = ET.SubElement(exq, "rate")
    ET.SubElement(rate, "timebase").text = "30"
    ET.SubElement(rate, "ntsc").text = "FALSE"
    exv = ET.SubElement(ET.SubElement(exq, "media"), "video")
    sc = ET.SubElement(ET.SubElement(exv, "format"), "samplecharacteristics")
    ET.SubElement(sc, "width").text = "1920"
    ET.SubElement(sc, "height").text = "1080"
    ex_tracks = [ET.SubElement(exv, "track") for _ in range(3)]
    for k, (a, b) in enumerate([(30, 60), (65, 90), (300, 330)]):
        gi = ET.SubElement(ex_tracks[1], "generatoritem")
        ET.SubElement(gi, "start").text = str(a)
        ET.SubElement(gi, "end").text = str(b)
    avatar = OUT / "avatar.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=1920x1080", "-frames:v", "1", str(avatar)], check=True)
    ex_xml = OUT / "export.xml"
    ET.ElementTree(export).write(ex_xml, encoding="utf-8")

    def avatars(*extra):
        out = OUT / "avatars.xml"
        sh("avatar_tracks.py", "--xml", ex_xml, "--map", f"V2=V3:{avatar}", "--out", out, *extra)
        items = ET.parse(out).getroot().findall("sequence/media/video/track")[2].findall("clipitem")
        return [(i.find("enabled").text, int(i.find("start").text), int(i.find("end").text)) for i in items]
    check(avatars() == [("FALSE", 0, 1), ("TRUE", 30, 90), ("TRUE", 300, 330)],
          "頭像：相近的字卡合併，00:00 有停用的對齊物件")
    check(avatars("--exclude", "300-330") == [("FALSE", 0, 1), ("TRUE", 30, 90)], "頭像：排除的字卡不放")

    # language profiles
    from common import load_config
    zh = load_config()
    check(zh["language"] == "zh" and zh["chinese_convert"] == "s2twp" and "笑死" in zh["chat_keywords"]
          and zh["whisper"]["model"] == "large-v3", "語言設定：預設是台灣中文，其他預設還在")
    lang_cfg = OUT / "lang.json"
    lang_cfg.write_text(json.dumps({"language_profile": "en", "chat_keywords": {"gg": 1.0}}), encoding="utf-8")
    en = load_config(lang_cfg)
    check(en["language"] == "en" and not en["chinese_convert"] and "lmao" in en["chat_keywords"] and "gg" in en["chat_keywords"]
          and "笑死" not in en["chat_keywords"], "語言設定：可以換語言，使用者設定優先")

    card = OUT / "card.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:s=1080x1920",
                    "-frames:v", "1", str(card)], check=True)
    p3_cfg = OUT / "p3_config.json"
    p3_cfg.write_text(json.dumps({"vertical_layout": {"template_png": str(card)}, "premiere": {
        "vertical": "editable", "cards": [
            {"match": "驚嚇卡", "file": str(card), "place": "at", "duration_sec": 2},
            {"match": "放大臉", "file": str(card), "place": "end", "duration_sec": 5}]}}), encoding="utf-8")
    p3_xml = OUT / "p3.xml"
    sh("export_premiere_xml.py", "--workdir", W, "--config", p3_cfg, "--out", p3_xml)
    p3 = {x.find("name").text[:3]: x for x in ET.parse(p3_xml).getroot().iter("sequence")}
    pv = p3["v01"]
    tracks = pv.findall("media/video/track")
    check(pv.find("media/video/format/samplecharacteristics/width").text == "1080" and len(tracks) == 4,
          f"可調整直式：1080 寬，主畫面、子母畫面、版型、字卡共 4 軌（{len(tracks)}）")
    first = tracks[0].find("clipitem")
    check(first.find("in").text == str(round(97.7 * 30)) and first.find("file/pathurl") is not None,
          "可調整直式：主畫面用原始直播檔與原始進點")
    check({e.text for e in first.iter("effectid")} == {"crop", "basic"}, "可調整直式：帶裁切與位置")
    check(tracks[3].find("clipitem/start").text == str(round((103 - 97.7) * 30)), "字卡放在標記時間")
    check(p3["h01"].find("duration").text == str(round(29.5 * 30) + 150), "片尾卡接在橫式最後，序列變長")

    sh("analyze_reference.py", "--out", OUT / "ref", W / "clips" / "v01_打王暴斃.mp4")
    rep = (OUT / "ref" / "report.md").read_text(encoding="utf-8")
    check("328–149" in rep, "參考成品分析：偵測到直式內容視窗 y≈328–1496")

    # refined transcript (as refine_transcript.py would write it) must replace the base text
    sys.path.insert(0, str(SCRIPTS))
    from common import merge_windows
    check(merge_windows([(100, 104), (106, 110), (200, 203)], 1) == [[99, 111], [199, 204]],
          "重轉區間合併正確")
    tx = "高品質重轉字幕"
    words = [{"start": 101 + i * 0.25, "end": 101.2 + i * 0.25, "word": ch} for i, ch in enumerate(tx)]
    (W / "transcript_refined.json").write_text(json.dumps(
        {"windows": [[100.5, 103.4]], "segments": [{"start": 101, "end": words[-1]["end"], "text": tx,
                                                    "words": words}]}, ensure_ascii=False), encoding="utf-8")
    mp4 = W / "clips" / "v01_打王暴斃.mp4"
    mtime = mp4.stat().st_mtime
    sh("cut_clips.py", "--workdir", W, "--only", "v01", "--srt-only")
    srt = (W / "clips" / "v01_打王暴斃.srt").read_text(encoding="utf-8-sig")
    check(tx in srt and "欸欸欸等一下等一下" not in srt and "不要不要" in srt,
          "字幕改用重轉結果，區間外維持原逐字稿")
    man = json.loads((W / "clips" / "manifest.json").read_text(encoding="utf-8"))
    check(mp4.stat().st_mtime == mtime and [c["id"] for c in man["clips"]] == ["v01", "h01"],
          "--srt-only 不重出影片，--only 不會弄丟其他片的 manifest")

    # cutting pauses inside segments
    from tighten import pauses, tighten_clip
    from validate_selections import cuts_speech
    ws = [{"start": 10.0, "end": 11.0, "word": "a"}, {"start": 13.0, "end": 14.0, "word": "b"},
          {"start": 14.2, "end": 15.0, "word": "c"}, {"start": 18.0, "end": 19.0, "word": "d"}]
    check(pauses(9, 20, ws, 0.8, 0.2, {}, 3) == [(9, 9.8), (11.2, 12.8), (15.2, 17.8), (19.2, 20)],
          "剪掉停頓：長停頓剪掉並留空氣，短間隔不動，頭尾的空白也剪")
    energy = {t: -30.0 for t in range(0, 30)}
    energy[16] = -20.0
    check((15.2, 17.8) not in pauses(9, 20, ws, 0.8, 0.2, energy, 3), "剪掉停頓：停頓裡比說話大聲（笑聲）就保留")
    tc = {"id": "v", "format": "vertical", "segments": [{"in": 9, "out": 20, "crop": "face"}],
          "notes": [{"at": 12, "text": "被剪掉的時間"}, {"at": 13.5, "text": "原本就在"}]}
    tsg = [{"start": 10.0, "end": 19.0, "text": "abcd", "words": ws}]
    segs, notes, removed = tighten_clip(tc, tsg, {}, {"tighten": {"min_gap_sec": 0.8, "pad_sec": 0.2}})
    check(len(segs) == 3 and all(s["crop"] == "face" for s in segs) and abs(removed - 5.8) < 0.01,
          f"剪掉停頓：一段變三段跳剪，裁切保留（{segs}，{removed}）")
    check(notes[0]["at"] == "00:00:12.800" and notes[1] == tc["notes"][1], "剪掉停頓：落在剪掉處的標記移到下一段開頭")
    check(not cuts_speech(tsg, 12.8) and cuts_speech(tsg, 10.5) and cuts_speech(tsg, 14.1),
          "選片檢查：剪在字之間的長停頓不算切在句子中間，切到字或短間隔才算")

    # subtitle splitting: never across Whisper segments, long phrases split evenly
    from cut_clips import build_cues
    from transcribe import fix_words

    def seg(t, text):
        ws = [{"start": t + i * 0.2, "end": t + i * 0.2 + 0.15, "word": ch} for i, ch in enumerate(text)]
        return {"start": t, "end": ws[-1]["end"], "text": text, "words": ws}
    cues = build_cues([seg(0, "今天都有在看欸"), seg(1.5, "真的假的"), seg(2.3, "你比我老"),
                       seg(3.5, "再來"), seg(4.0, "再來"),
                       seg(5, "今天天氣很好我們一起去公園散步吧")], [{"a": 0, "b": 10}], 12, 0.5)
    got = [c[2] for c in cues]
    check(got[:4] == ["今天都有在看欸", "真的假的", "你比我老", "再來 再來"] and len(got) == 6 and
          "".join(got[4:]) == "今天天氣很好我們一起去公園散步吧" and all(len(x) <= 12 for x in got[4:]),
          f"字幕在句子邊界斷開、短句合併、長句切成兩行（{got}）")
    fw = fix_words([{"word": "小", "start": 0, "end": 1}, {"word": "銘", "start": 1, "end": 2}], [("小銘", "小明")])
    check(fw == [{"word": "小明", "start": 0, "end": 2}], "固定替換可跨字元 token（小+銘 → 小明）")
    from common import default_workdir, find_config, load_json
    root = OUT / "素材"
    (root / "20260101" / "20260101_精華").mkdir(parents=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "20260101" / "直播 🎮.mp4").write_bytes(b"")
    check(find_config(root / "20260101" / "20260101_精華") == str((root / "config.json").resolve()),
          "設定檔：從 WORK 往上兩層找到 <素材根目錄>/config.json")
    check(default_workdir(root / "20260101" / "直播 🎮.mp4").name == "20260101_精華"
          and default_workdir(OUT / "直播 🎮.mp4").name == "直播 🎮_精華",
          "工作資料夾：日期資料夾用 <日期>_精華，否則用 <檔名>_精華")
    (OUT / "bom.json").write_bytes('{"streamer_name": "小明"}'.encode("utf-8-sig"))
    check(load_json(OUT / "bom.json")["streamer_name"] == "小明", "讀得到記事本存的 UTF-8 BOM 設定檔")
    print("\n全部通過。輸出在", OUT)


if __name__ == "__main__":
    main()
