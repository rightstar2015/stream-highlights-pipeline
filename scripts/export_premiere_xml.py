"""Export selections as Final Cut Pro 7 XML that Premiere Pro imports (File > Import).

Usage: python export_premiere_xml.py --workdir WORK [--out highlights_premiere.xml]

- Horizontal clips: one sequence each, built from the ORIGINAL stream file with the
  chosen in/out points (multi-segment = cuts already laid end to end). You keep
  full-quality media and can extend any cut with the trim tool.
- Vertical clips: one 1080x1920 sequence each containing the rendered vertical MP4
  from cut_clips.py (layout already applied). Run cut_clips.py first.
Sequence names start with the clip id and title so they sort in the bin.
"""
import argparse
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from common import load_json, normalize_segments, parse_ts, probe, source_to_clip_time, work_paths


def bmp_only(text):
    """Premiere's XML importer fails ("general error") on characters outside the BMP, e.g. emoji like 🎮."""
    return re.sub(r" {2,}", " ", re.sub(r"[𐀀-􏿿]", "", text)).strip()


def bmp_safe_source(src):
    """Hard-link the stream under a name without non-BMP characters (same data, no extra disk space)."""
    safe = src.with_name(bmp_only(src.stem) + src.suffix)
    if safe == src:
        return src
    if safe.exists():
        if not os.path.samefile(safe, src):
            raise SystemExit(f"[錯誤] {safe} 已存在但不是直播檔的硬連結，請手動處理")
    else:
        try:
            os.link(src, safe)
        except OSError as e:
            raise SystemExit(f"[錯誤] 直播檔名含 emoji，Premiere 無法匯入；建立硬連結失敗（{e}），請把檔名的 emoji 拿掉再執行")
        print(f"[提示] 直播檔名含 emoji，Premiere 讀不了，已建立硬連結（不佔額外空間）→ {safe.name}")
    return safe


def path_url(p):
    uri = Path(p).resolve().as_uri()  # file:///C:/... or file:///home/...
    return "file://localhost" + uri[len("file://"):]


def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = bmp_only(str(text)) if tag in ("name", "comment") else str(text)
    return el


def rate_el(parent, fps_num, fps_den):
    r = sub(parent, "rate")
    ntsc = fps_den == 1001
    sub(r, "timebase", round(fps_num / fps_den))
    sub(r, "ntsc", "TRUE" if ntsc else "FALSE")
    return r


class FileRegistry:
    def __init__(self):
        self.ids = {}

    def file_el(self, parent, path, info, fps):
        key = str(Path(path).resolve())
        if key in self.ids:
            sub(parent, "file", id=self.ids[key])
            return
        fid = f"file-{len(self.ids) + 1}"
        self.ids[key] = fid
        f = sub(parent, "file", id=fid)
        sub(f, "name", Path(path).name)
        sub(f, "pathurl", path_url(path))
        rate_el(f, *fps)
        sub(f, "duration", int(info["duration"] * info["fps"]))
        media = sub(f, "media")
        v = sub(media, "video")
        sc = sub(v, "samplecharacteristics")
        rate_el(sc, *fps)
        sub(sc, "width", info["width"])
        sub(sc, "height", info["height"])
        sub(sc, "anamorphic", "FALSE")
        sub(sc, "pixelaspectratio", "square")
        sub(sc, "fielddominance", "none")
        if "audio_channels" in info:
            a = sub(media, "audio")
            asc = sub(a, "samplecharacteristics")
            sub(asc, "depth", 16)
            sub(asc, "samplerate", info.get("audio_rate", 48000))
            sub(a, "channelcount", info["audio_channels"])


def add_markers(seq, notes, fps):
    """notes: list of (seconds_on_timeline, text) -> sequence markers."""
    fr = fps[0] / fps[1]
    for t, text in notes:
        mk = sub(seq, "marker")
        sub(mk, "comment", "")
        sub(mk, "name", text)
        sub(mk, "in", int(round(t * fr)))
        sub(mk, "out", -1)


def build_sequence(root_children, reg, name, seq_w, seq_h, fps, pieces, uid, notes=()):
    """pieces: list of (path, info, src_in_sec, src_out_sec); notes: (timeline_sec, text)."""
    fr = fps[0] / fps[1]
    seq = sub(root_children, "sequence", id=f"sequence-{uid}")
    sub(seq, "name", name)
    total = 0
    frames = []
    for _, _, a, b in pieces:
        i, o = int(round(a * fr)), int(round(b * fr))
        frames.append((i, o, total))
        total += o - i
    sub(seq, "duration", total)
    rate_el(seq, *fps)
    tc = sub(seq, "timecode")
    rate_el(tc, *fps)
    sub(tc, "string", "00:00:00:00")
    sub(tc, "frame", 0)
    sub(tc, "displayformat", "NDF")
    media = sub(seq, "media")
    video = sub(media, "video")
    fmt = sub(video, "format")
    sc = sub(fmt, "samplecharacteristics")
    rate_el(sc, *fps)
    sub(sc, "width", seq_w)
    sub(sc, "height", seq_h)
    sub(sc, "anamorphic", "FALSE")
    sub(sc, "pixelaspectratio", "square")
    sub(sc, "fielddominance", "none")
    vtrack = sub(video, "track")
    audio = sub(media, "audio")
    sub(audio, "numOutputChannels", 2)
    afmt = sub(audio, "format")
    asc = sub(afmt, "samplecharacteristics")
    sub(asc, "depth", 16)
    sub(asc, "samplerate", 48000)
    atracks = [sub(audio, "track"), sub(audio, "track")]

    for k, ((path, info, a, b), (fi, fo, start)) in enumerate(zip(pieces, frames)):
        end = start + (fo - fi)
        vid, aid = f"{uid}-v{k}", f"{uid}-a{k}"
        has_audio = "audio_channels" in info
        ci = sub(vtrack, "clipitem", id=vid)
        sub(ci, "name", Path(path).name)
        sub(ci, "enabled", "TRUE")
        sub(ci, "duration", int(info["duration"] * info["fps"]))
        rate_el(ci, *fps)
        sub(ci, "start", start)
        sub(ci, "end", end)
        sub(ci, "in", fi)
        sub(ci, "out", fo)
        reg.file_el(ci, path, info, fps)
        if has_audio:
            for lid, mt, idx in ((vid, "video", 1), (f"{aid}-1", "audio", 1), (f"{aid}-2", "audio", 2)):
                ln = sub(ci, "link")
                sub(ln, "linkclipref", lid)
                sub(ln, "mediatype", mt)
                sub(ln, "trackindex", idx)
                sub(ln, "clipindex", k + 1)
            for ch in (1, 2):
                ac = sub(atracks[ch - 1], "clipitem", id=f"{aid}-{ch}")
                sub(ac, "name", Path(path).name)
                sub(ac, "enabled", "TRUE")
                sub(ac, "duration", int(info["duration"] * info["fps"]))
                rate_el(ac, *fps)
                sub(ac, "start", start)
                sub(ac, "end", end)
                sub(ac, "in", fi)
                sub(ac, "out", fo)
                sub(ac, "file", id=reg.ids[str(Path(path).resolve())])
                st = sub(ac, "sourcetrack")
                sub(st, "mediatype", "audio")
                sub(st, "trackindex", min(ch, info["audio_channels"]))
                for lid, mt, idx in ((vid, "video", 1), (f"{aid}-1", "audio", 1), (f"{aid}-2", "audio", 2)):
                    ln = sub(ac, "link")
                    sub(ln, "linkclipref", lid)
                    sub(ln, "mediatype", mt)
                    sub(ln, "trackindex", idx)
                    sub(ln, "clipindex", k + 1)
    add_markers(seq, notes, fps)
    return seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    paths = work_paths(args.workdir)
    sel = load_json(paths["selections"])
    src = bmp_safe_source(Path(sel["source"]))
    sinfo = probe(src)
    sfps = (sinfo["fps_num"], sinfo["fps_den"])
    manifest = {}
    mpath = paths["clips"] / "manifest.json"
    if mpath.exists():
        manifest = {c["id"]: c for c in load_json(mpath)["clips"]}

    root = ET.Element("xmeml", version="4")
    project = sub(root, "project")
    sub(project, "name", f"精華_{src.stem}")
    children = sub(project, "children")
    reg = FileRegistry()
    made, skipped = [], []
    for n, clip in enumerate(sel["clips"], 1):
        name = f"{clip['id']}_{clip.get('title', '')}".strip("_")
        if clip["format"] == "horizontal":
            segs = normalize_segments(clip)
            pieces = [(src, sinfo, s["a"], s["b"]) for s in segs]
            notes = []
            for nt in clip.get("notes", []):
                t = source_to_clip_time(segs, parse_ts(nt["at"]))
                if t is not None:
                    notes.append((t, nt["text"]))
            build_sequence(children, reg, name, sinfo["width"], sinfo["height"], sfps, pieces, n, notes)
            made.append(name)
        else:
            m = manifest.get(clip["id"])
            if not m or not Path(m["file"]).exists() or not m["file"].endswith(".mp4"):
                skipped.append(clip["id"])
                continue
            vinfo = probe(m["file"])
            vfps = (vinfo["fps_num"], vinfo["fps_den"])
            build_sequence(children, reg, name, vinfo["width"], vinfo["height"], vfps,
                           [(m["file"], vinfo, 0.0, vinfo["duration"])], n,
                           [(x["t"], x["text"]) for x in m.get("notes", [])]
                           + ([(0.0, "標題：" + m["header_title"])] if m.get("header_title") else []))
            made.append(name)

    out = Path(args.out) if args.out else paths["dir"] / f"{bmp_only(src.stem)}_premiere.xml"
    ET.indent(root)
    xml = ET.tostring(root, encoding="unicode")
    with open(out, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + xml + "\n")
    print(f"已建立 {len(made)} 個序列 → {out}")
    if skipped:
        print(f"[提示] 這些直式片段還沒輸出 MP4，先執行 cut_clips.py：{', '.join(skipped)}")


if __name__ == "__main__":
    main()
