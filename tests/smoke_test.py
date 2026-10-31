"""End-to-end smoke test with a synthetic 5-minute stream. Run after every change:

    python tests/smoke_test.py            (keeps output in tests/_out for inspection)

Needs only ffmpeg + numpy (no whisper, no network). Checks:
  chat parsing, audio energy, signal merge (top candidate must land on the staged event),
  template vertical render (1080x1920), multi-segment horizontal render, SRT, loudness,
  Premiere XML structure + markers, reference analysis.
"""
import json
import shutil
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
             "--no-separate", "--frames", "3")
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
    srv.shutdown()

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
    check(names == ["放大臉"], f"標記點正確且剪掉的段落不出現（{names}）")
    v = [s for n, s in seqs.items() if n.startswith("v01")][0]
    check(any("標題：我破防了" == m.find("name").text for m in v.findall("marker")), "直式標題標記")

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
