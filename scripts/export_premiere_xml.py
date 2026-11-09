"""Export selections as Final Cut Pro 7 XML that Premiere Pro imports (File > Import).

Usage: python export_premiere_xml.py --workdir WORK [--config config.json] [--vertical mp4|editable]
                                     [--out highlights_premiere.xml]

- Horizontal clips: one sequence each, built from the ORIGINAL stream file with the
  chosen in/out points (multi-segment = cuts already laid end to end). You keep
  full-quality media and can extend any cut with the trim tool.
- Vertical clips: one 1080x1920 sequence each containing the rendered vertical MP4
  from cut_clips.py (layout already applied). Run cut_clips.py first.
- --vertical editable (template layout only): the original stream with Crop and Basic Motion, plus
  pip and template PNG on upper tracks, so cuts can be extended. Other layouts fall back to the MP4.
- premiere.cards in the config: a note whose text contains "match" also places that file, either
  over the picture at the note ("place": "at") or after the last cut ("place": "end").
- The SRT can't be carried in the XML, so a marker at 00:00 names the file to import.
Sequence names start with the clip id and title so they sort in the bin.
"""
import argparse
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from common import (SKILL_DIR, atomic_write, find_config, load_config, load_json, normalize_segments, parse_ts, probe,
                    source_to_clip_time, vfr_warning, work_paths)
from cut_clips import clamp_crop, resolve_template

STILL_SUFFIXES = {".png", ".jpg", ".jpeg"}
STILL_DURATION_SEC = 4 * 3600  # length of the "media" behind a still; only needs to exceed any clip


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
            raise SystemExit(f"[錯誤] 直播檔名含 emoji，Premiere 無法匯入；建立硬連結失敗（{e}），請把檔名的 emoji 拿掉再執行") from e
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


def param(effect, pid, name, value=None, vmin=None, vmax=None):
    pr = sub(effect, "parameter")
    sub(pr, "parameterid", pid)
    sub(pr, "name", name)
    if vmin is not None:
        sub(pr, "valuemin", vmin)
        sub(pr, "valuemax", vmax)
    return sub(pr, "value", None if value is None else f"{value:.4f}")


def effect_el(parent, name, effect_id):
    e = sub(sub(parent, "filter"), "effect")
    sub(e, "name", name)
    sub(e, "effectid", effect_id)
    sub(e, "effectcategory", "motion")
    sub(e, "effecttype", "motion")
    sub(e, "mediatype", "video")
    return e


def add_placement(ci, pl, sw, sh):
    """Crop + Basic Motion. Center is the offset from the frame centre as a fraction of the source size."""
    c = effect_el(ci, "Crop", "crop")
    for side in ("left", "right", "top", "bottom"):
        param(c, side, side, pl["crop"][side], 0, 100)
    m = effect_el(ci, "Basic Motion", "basic")
    param(m, "scale", "Scale", pl["scale"], 0, 1000)
    v = param(m, "center", "Center")
    sub(v, "horiz", f"{pl['dx'] / sw:.6f}")
    sub(v, "vert", f"{pl['dy'] / sh:.6f}")


def place_region(region, sw, sh, box):
    """Same result as ffmpeg's crop, scale-to-fill and centre crop: fit source region into box (x, y, w, h).
    -> {"scale": percent, "cx"/"cy": where the source centre lands in the sequence, "crop": percents}"""
    rw, rh, rx, ry = clamp_crop(region, sw, sh)
    x, y, w, h = box
    s = max(w / rw, h / rh)
    vw, vh = w / s, h / s
    vx, vy = rx + (rw - vw) / 2, ry + (rh - vh) / 2
    return {"scale": s * 100,
            "cx": x + w / 2 - (vx + vw / 2 - sw / 2) * s, "cy": y + h / 2 - (vy + vh / 2 - sh / 2) * s,
            "crop": {"left": vx / sw * 100, "right": (sw - vx - vw) / sw * 100,
                     "top": vy / sh * 100, "bottom": (sh - vy - vh) / sh * 100}}


def vertical_placements(lay, sw, sh, crop):
    """-> (main placement, pip placement or None) for one segment of the template layout."""
    W, H = lay["output_size"]
    X, Y, CW, CH = lay["content_box"]
    name = crop or lay.get("default_crop", "game")
    region = name if isinstance(name, (list, dict)) else lay.get("crop_presets", {}).get(name)
    if region is None:
        raise SystemExit(f"[錯誤] 找不到裁切預設「{name}」，請在 config 的 crop_presets 定義")
    pip = None
    if isinstance(region, dict):
        pip, region = region.get("pip"), region["region"]
    out = [place_region(region, sw, sh, (X, Y, CW, CH)), None]
    if pip:
        pw, ph, _, _ = clamp_crop(pip["region"], sw, sh)
        ax, ay, aw = pip["at"]
        out[1] = place_region(pip["region"], sw, sh, (X + ax, Y + ay, aw, aw * ph / pw))
    for pl in out:
        if pl:
            pl["dx"], pl["dy"] = pl["cx"] - W / 2, pl["cy"] - H / 2
    return out


def media_item(path, fps):
    """(path, info, is_still) for a template or card file; stills get a long stand-in duration."""
    path = bmp_safe_source(Path(path))
    info = probe(path)
    still = path.suffix.lower() in STILL_SUFFIXES
    if still:
        info = dict(info, duration=STILL_DURATION_SEC, fps=fps[0] / fps[1], fps_num=fps[0], fps_den=fps[1])
        info.pop("audio_channels", None)
    return path, info, still


def layer_clip(track, reg, cid, item, start, end, fps, src_in=0, placement=None):
    path, info, still = item
    ci = sub(track, "clipitem", id=cid)
    sub(ci, "name", Path(path).name)
    sub(ci, "enabled", "TRUE")
    sub(ci, "duration", int(info["duration"] * fps[0] / fps[1]))
    rate_el(ci, *fps)
    sub(ci, "start", start)
    sub(ci, "end", end)
    sub(ci, "in", src_in)
    sub(ci, "out", src_in + end - start)
    if still:
        sub(ci, "alphatype", "straight")
    reg.file_el(ci, path, info, (info["fps_num"], info["fps_den"]))
    if placement:
        add_placement(ci, placement, info["width"], info["height"])
    return ci


def card_plan(notes, cards, total_sec):
    """Match clip notes to config cards. notes: (timeline_sec or None if cut out, text).
    -> (cards over the picture as [(sec, card)], end card or None)."""
    over, end = [], None
    for t, text in notes:
        for card in cards:
            if card.get("match") and card["match"] in text:
                if card.get("place") == "end":
                    end = end or card
                elif t is not None and t < total_sec:
                    over.append((t, card))
                break
    return sorted(over, key=lambda x: x[0]), end


def resolve_card(card):
    p = Path(card.get("file") or "")
    if not p.is_absolute():
        p = SKILL_DIR / p
    if not p.is_file():
        print(f"[提示] 找不到「{card.get('match')}」的素材：{p}，這張卡只保留標記")
        return None
    return p


def build_sequence(root_children, reg, name, seq_w, seq_h, fps, pieces, uid, notes=(),
                   placements=None, pip=None, template=None, over_cards=(), end_card=None):
    """pieces: list of (path, info, src_in_sec, src_out_sec); notes: (timeline_sec, text).
    placements/pip: per-piece placement or None; template: media_item over the whole picture;
    over_cards: (timeline_sec, duration_sec, media_item); end_card: (duration_sec, media_item)."""
    fr = fps[0] / fps[1]
    seq = sub(root_children, "sequence", id=f"sequence-{uid}")
    sub(seq, "name", name)
    total = 0
    frames = []
    for _, _, a, b in pieces:
        i, o = int(round(a * fr)), int(round(b * fr))
        frames.append((i, o, total))
        total += o - i
    end_frames = int(round(end_card[0] * fr)) if end_card else 0
    sub(seq, "duration", total + end_frames)
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

    for k, ((path, info, _, _), (fi, fo, start)) in enumerate(zip(pieces, frames)):
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
        if placements and placements[k]:
            add_placement(ci, placements[k], info["width"], info["height"])
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
    if end_card:
        item = end_card[1]
        layer_clip(vtrack, reg, f"{uid}-end", item, total, total + end_frames, fps)
        if "audio_channels" in item[1]:
            for ch in (1, 2):
                layer_clip(atracks[ch - 1], reg, f"{uid}-end-a{ch}", item, total, total + end_frames, fps)
    if pip and any(pip):
        track = sub(video, "track")
        for k, ((path, info, _, _), (fi, fo, start)) in enumerate(zip(pieces, frames)):
            if pip[k]:
                layer_clip(track, reg, f"{uid}-pip{k}", (path, info, False), start, start + fo - fi, fps, fi, pip[k])
    if template:
        layer_clip(sub(video, "track"), reg, f"{uid}-tpl", template, 0, total, fps)
    if over_cards:
        track = sub(video, "track")
        starts = [int(round(t * fr)) for t, _, _ in over_cards] + [total]
        for k, (_, dur, item) in enumerate(over_cards):
            end = min(starts[k] + int(round(dur * fr)), starts[k + 1])
            if end > starts[k]:
                layer_clip(track, reg, f"{uid}-card{k}", item, starts[k], end, fps)
    add_markers(seq, notes, fps)
    return seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--vertical", choices=["mp4", "editable"], help="直式序列放已輸出的 MP4，或原始直播檔加裁切與版型")
    ap.add_argument("--out")
    args = ap.parse_args()
    paths = work_paths(args.workdir)
    sel = load_json(paths["selections"])
    src = bmp_safe_source(Path(sel["source"]))
    cfg = load_config(args.config or find_config(paths["dir"], src.parent))
    pcfg = cfg.get("premiere", {})
    vertical_mode = args.vertical or pcfg.get("vertical", "mp4")
    cards = pcfg.get("cards", [])
    sinfo = probe(src)
    if vfr_warning(sinfo):
        print("[提示] " + vfr_warning(sinfo))
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

    def card_args(clip, segs, fps, total):
        """Note times are source times; a note outside the kept segments can still ask for the end card."""
        found = [(source_to_clip_time(segs, parse_ts(nt["at"])), nt["text"]) for nt in clip.get("notes", [])]
        over, end = card_plan(found, cards, total)
        over_cards = []
        for t, card in over:
            p = resolve_card(card)
            if p:
                over_cards.append((t, float(card.get("duration_sec", 2)), media_item(p, fps)))
        end_card = None
        p = resolve_card(end) if end else None
        if p:
            item = media_item(p, fps)
            default = 5.0 if item[2] else item[1]["duration"]
            end_card = (float(end.get("duration_sec", default)), item)
        return {"over_cards": over_cards, "end_card": end_card}

    def start_marker(clip, entry, notes):
        """One marker at 00:00 for the title and the SRT to import."""
        parts = []
        title = entry.get("header_title") or clip.get("header_title")
        if title:
            parts.append("標題：" + title)
        if entry.get("srt"):
            parts.append("字幕：匯入 " + Path(entry["srt"]).name)
        return ([(0.0, "｜".join(parts))] if parts else []) + notes

    for n, clip in enumerate(sel["clips"], 1):
        name = f"{clip['id']}_{clip.get('title', '')}".strip("_")
        m = manifest.get(clip["id"], {})
        segs = normalize_segments(clip)
        lay = dict(cfg["vertical_layout"])
        lay.update(clip.get("layout", {}))
        editable = clip["format"] == "vertical" and vertical_mode == "editable"
        if editable and lay.get("mode", "template") != "template":
            print(f"[提示] {clip['id']}：只有 template 版型能做成可調整序列，這支改用 MP4")
            editable = False
        if clip["format"] == "horizontal" or editable:
            pieces = [(src, sinfo, s["a"], s["b"]) for s in segs]
            notes = []
            for nt in clip.get("notes", []):
                t = source_to_clip_time(segs, parse_ts(nt["at"]))
                if t is not None:
                    notes.append((t, nt["text"]))
            extra = card_args(clip, segs, sfps, sum(s["b"] - s["a"] for s in segs))
            w, h = sinfo["width"], sinfo["height"]
            if editable:
                w, h = lay["output_size"]
                placed = [vertical_placements(lay, sinfo["width"], sinfo["height"], s.get("crop")) for s in segs]
                tpl = resolve_template(lay)
                extra.update(placements=[x[0] for x in placed], pip=[x[1] for x in placed],
                             template=media_item(tpl, sfps) if tpl else None)
            build_sequence(children, reg, name, w, h, sfps, pieces, n, start_marker(clip, m, notes), **extra)
            made.append(name)
        else:
            if not m or not Path(m["file"]).exists() or not m["file"].endswith(".mp4"):
                skipped.append(clip["id"])
                continue
            vinfo = probe(m["file"])
            vfps = (vinfo["fps_num"], vinfo["fps_den"])
            extra = card_args(clip, segs, vfps, vinfo["duration"])
            build_sequence(children, reg, name, vinfo["width"], vinfo["height"], vfps,
                           [(m["file"], vinfo, 0.0, vinfo["duration"])], n,
                           start_marker(clip, m, [(x["t"], x["text"]) for x in m.get("notes", [])]), **extra)
            made.append(name)

    out = Path(args.out) if args.out else paths["dir"] / f"{bmp_only(src.stem)}_premiere.xml"
    ET.indent(root)
    xml = ET.tostring(root, encoding="unicode")
    with atomic_write(out) as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + xml + "\n")
    print(f"已建立 {len(made)} 個序列 → {out}")
    if skipped:
        print(f"[提示] 這些直式片段還沒輸出 MP4，先執行 cut_clips.py：{', '.join(skipped)}")


if __name__ == "__main__":
    main()
