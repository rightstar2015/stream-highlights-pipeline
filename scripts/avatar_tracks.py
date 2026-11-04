"""Place speaker avatars on video tracks, timed from per-speaker subtitle tracks.

Usage:
  python avatar_tracks.py --xml 匯出的序列.xml [--config config.json] [--sequence 序列名稱]
                          [--map V5=V3 --map V6=V4:頭像/來賓B.png]
                          [--gap 0.5] [--exclude 716-820 ...] [--out 輸出.xml]

The editor splits subtitle cards by speaker onto their own tracks in Premiere (e.g. V5 = guest A,
V6 = guest B) and exports the sequence (File > Export > Final Cut Pro XML). For every card on a
source track, this puts that speaker's avatar on the target track for the same time span.
Cards of the same speaker separated by a gap of --gap seconds or less become one avatar clip, so
the avatar does not flicker between lines. --exclude drops cards that sit on a speaker's track but
belong to someone else (frames "716-820" or timecodes "00:00:11:56-00:00:13:40").

Avatar image: when the target track already has an avatar the editor placed by hand, that clip is
the template: same image, same Motion (position / scale). The image part of --map is then
optional. Otherwise give a PNG, best made at the sequence size (1920x1080, transparent) with the
avatar already in place. Only empty stretches of the target track are filled, so hand-placed
avatars stay as they are. Effects other than Motion (e.g. 投影) do not survive FCP XML; re-apply
them with Paste Attributes.

Output: an XML with ONE sequence holding only the new avatar clips (same frame rate and size as the
exported sequence). Premiere pastes the earliest copied clip at the playhead, not at its absolute
time, so a disabled 1-frame anchor clip is put at 00:00 on the lowest avatar track, or on the next
track up that is empty at 00:00 in the original: import, select all, copy, then in the original
sequence put the playhead at 00:00, target the lowest avatar track, paste, and delete the anchor.
When no such track exists, the playhead goes to the printed timecode of the first avatar instead. The original
sequence is never round-tripped through XML, so text graphics / MOGRT captions stay untouched.

Mapping comes from --map (repeatable, "來源軌=目標軌[:圖片]") or config "avatar_tracks.map".
"""
import argparse
import copy
import struct
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from common import atomic_write, find_config, load_config
from export_premiere_xml import bmp_only, path_url, rate_el, sub

ANCHOR_NAME = "對齊用_貼上後刪除"
STILL_DURATION_SEC = 4 * 3600  # length of the "media" behind each still; only needs to exceed any clip
MIN_FILL_SEC = 0.3  # leftover slivers next to hand-placed avatars shorter than this are dropped


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"[錯誤] 頭像要用 PNG（透明背景）：{path}")
    return struct.unpack(">II", head[16:24])


def timecode(frame, fps):
    tb = round(fps[0] / fps[1])
    sec, ff = divmod(frame, tb)
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}:{ff:02d}"


def parse_frame(text, fps):
    text = text.strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if len(parts) != 4 or not all(x.isdigit() for x in parts):
        raise SystemExit(f"[錯誤] 時間要寫成影格數或 HH:MM:SS:FF（收到：{text}）")
    h, m, sec, ff = map(int, parts)
    return (h * 3600 + m * 60 + sec) * round(fps[0] / fps[1]) + ff


def parse_ranges(items, fps):
    out = []
    for it in items:
        a, sep, b = it.partition("-")
        if not sep:
            raise SystemExit(f"[錯誤] --exclude 格式是「開始-結束」，例如 716-820（收到：{it}）")
        out.append((parse_frame(a, fps), parse_frame(b, fps)))
    return out


def occupied_at_zero(track):
    for el in track:
        if el.tag in ("clipitem", "generatoritem") and int(el.findtext("start") or -1) <= 0 < int(el.findtext("end") or -1):
            return True
    return False


def parse_map(items):
    out = []
    for it in items:
        if "=" not in it:
            raise SystemExit(f"[錯誤] --map 格式是「來源軌=目標軌[:圖片]」，例如 V5=V3 或 V5=V3:頭像/來賓A.png（收到：{it}）")
        src, rest = it.split("=", 1)
        dst, _, image = rest.partition(":")
        out.append({"from": src, "to": dst, "image": image or None})
    return out


def track_no(name):
    n = str(name).strip().upper().lstrip("V")
    if not n.isdigit() or int(n) < 1:
        raise SystemExit(f"[錯誤] 軌道要寫成 V1、V2…（收到：{name}）")
    return int(n)


def pick_sequence(root, name):
    seqs = [s for s in root.iter("sequence") if s.find("media/video") is not None]
    if name:
        for s in seqs:
            if (s.findtext("name") or "") == name:
                return s
        raise SystemExit(f"[錯誤] XML 裡找不到序列「{name}」，有：{', '.join(s.findtext('name') or '?' for s in seqs)}")
    top = root.find("sequence")  # Premiere puts the exported sequence at the root; nested ones live in clips
    if top is None and seqs:
        top = seqs[0]
    if top is None:
        raise SystemExit("[錯誤] XML 裡沒有序列，請用「檔案 > 匯出 > Final Cut Pro XML」匯出")
    return top


def seq_rate(seq):
    r = seq.find("rate")
    tb = int(r.findtext("timebase"))
    ntsc = (r.findtext("ntsc") or "FALSE").upper() == "TRUE"
    return (tb * 1000, 1001) if ntsc else (tb, 1)


def enabled_items(track):
    """(element, start, end) of every enabled item on a track. Items next to a transition export
    start/end as -1; their edge is the transition's own start/end."""
    items = [el for el in track if el.tag in ("clipitem", "generatoritem", "transitionitem")]
    out = []
    for i, el in enumerate(items):
        if el.tag == "transitionitem" or (el.findtext("enabled") or "TRUE").upper() == "FALSE":
            continue
        a, b = int(el.findtext("start")), int(el.findtext("end"))
        if a < 0 and i > 0 and items[i - 1].tag == "transitionitem":
            a = int(items[i - 1].findtext("start"))
        if b < 0 and i + 1 < len(items) and items[i + 1].tag == "transitionitem":
            b = int(items[i + 1].findtext("end"))
        if 0 <= a < b:
            out.append((el, a, b))
    return sorted(out, key=lambda x: x[1])


def merge_spans(spans, gap):
    out = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def subtract_spans(spans, taken, min_len):
    """Parts of `spans` not covered by `taken`, dropping pieces shorter than min_len frames."""
    out = []
    for a, b in spans:
        pieces = [(a, b)]
        for ta, tb in taken:
            pieces = [p for x, y in pieces
                      for p in ((x, min(y, ta)), (max(x, tb), y)) if p[0] < p[1]]
        out += [p for p in pieces if p[1] - p[0] >= min_len]
    return out


def file_path_of(clip):
    url = clip.findtext("file/pathurl") or ""
    if url.startswith("file://localhost/"):
        url = url[len("file://localhost/"):]
    elif url.startswith("file:///"):
        url = url[len("file:///"):]
    return Path(urllib.parse.unquote(url)) if url else None


def template_clip(track, files):
    """First hand-placed still on the target track, with its <file> resolved (Premiere writes the
    full <file> only at its first use in the XML and an id reference afterwards)."""
    for el, _, _ in enabled_items(track):
        if el.tag != "clipitem" or el.find("file") is None:
            continue
        f = el.find("file")
        full = f if f.find("pathurl") is not None else files.get(f.get("id"))
        if full is not None:
            t = copy.deepcopy(el)
            t.remove(t.find("file"))
            t.append(copy.deepcopy(full))
            return t
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True, help="Premiere 匯出的 Final Cut Pro XML")
    ap.add_argument("--config")
    ap.add_argument("--sequence", help="XML 裡有多個序列時指定名稱")
    ap.add_argument("--map", action="append", default=[], help="來源軌=目標軌[:圖片]，可重複")
    ap.add_argument("--gap", type=float, help="同一人兩句間隔幾秒內合併成一段頭像（預設 0.5）")
    ap.add_argument("--exclude", nargs="*", default=[], help="不放頭像的字卡時間，開始-結束（影格或 HH:MM:SS:FF）")
    ap.add_argument("--out")
    args = ap.parse_args()

    xml_path = Path(args.xml)
    cfg = load_config(args.config or find_config(xml_path.parent))
    acfg = cfg.get("avatar_tracks", {})
    mapping = parse_map(args.map) if args.map else acfg.get("map", [])
    if not mapping:
        raise SystemExit("[錯誤] 沒有軌道對應：用 --map V5=V3[:頭像.png]，或在 config 的 avatar_tracks.map 設定")
    gap_sec = args.gap if args.gap is not None else acfg.get("gap_merge_sec", 0.5)

    root_in = ET.parse(xml_path).getroot()
    files = {f.get("id"): f for f in root_in.iter("file") if f.find("pathurl") is not None}
    seq = pick_sequence(root_in, args.sequence)
    fps = seq_rate(seq)
    fr = fps[0] / fps[1]
    vtracks = seq.findall("media/video/track")
    sc = seq.find("media/video/format/samplecharacteristics")
    seq_w = int(sc.findtext("width")) if sc is not None else 1920
    seq_h = int(sc.findtext("height")) if sc is not None else 1080
    excludes = parse_ranges(args.exclude, fps)

    plan = []
    for m in mapping:
        src_no, dst_no = track_no(m["from"]), track_no(m["to"])
        if src_no > len(vtracks):
            raise SystemExit(f"[錯誤] 序列只有 V1–V{len(vtracks)}，沒有 V{src_no}")
        dst_track = vtracks[dst_no - 1] if dst_no <= len(vtracks) else None
        existing = [(a, b) for _, a, b in enabled_items(dst_track)] if dst_track is not None else []
        tmpl = template_clip(dst_track, files) if dst_track is not None else None
        label = m.get("name") or f"V{src_no}"

        if m.get("image"):
            image = Path(m["image"])
        elif tmpl is not None:
            image = file_path_of(tmpl)
        else:
            raise SystemExit(f"[錯誤] V{dst_no} 上沒有可沿用的頭像，請在 --map 指定圖片：V{src_no}=V{dst_no}:頭像.png")
        if not image or not image.is_file():
            raise SystemExit(f"[錯誤] 找不到頭像：{image}")
        size = None
        if tmpl is not None and file_path_of(tmpl) == image:
            fsc = tmpl.find("file/media/video/samplecharacteristics")
            if fsc is not None and fsc.findtext("width"):
                size = (int(fsc.findtext("width")), int(fsc.findtext("height")))
        if size is None:
            size = png_size(image)
        filters = [copy.deepcopy(f) for f in tmpl.findall("filter")] if tmpl is not None else []
        if filters:
            print(f"[{label}] 沿用 V{dst_no} 上已放好的頭像「{tmpl.findtext('name')}」的位置／縮放")
        elif size != (seq_w, seq_h):
            print(f"[提示] {image.name} 是 {size[0]}x{size[1]}，不是序列的 {seq_w}x{seq_h}，貼上後要手動調位置")

        cards = [(a, b) for _, a, b in enabled_items(vtracks[src_no - 1])]
        kept = subtract_spans(cards, excludes, round(MIN_FILL_SEC * fr))
        if len(kept) != len(cards) or kept != cards:
            print(f"[{label}] 排除 {', '.join(f'{timecode(a, fps)}–{timecode(b, fps)}' for a, b in excludes)} 的字卡")
        spans = merge_spans(kept, round(gap_sec * fr))
        fill = subtract_spans(spans, existing, round(MIN_FILL_SEC * fr))
        if existing:
            print(f"[{label}] V{dst_no} 已有 {len(existing)} 段頭像，只補空白處")
        plan.append(dict(label=label, src=src_no, dst=dst_no, image=image, size=size,
                         filters=filters, cards=len(cards), spans=fill, existing=existing))
    if len({p["dst"] for p in plan}) < len(plan):
        raise SystemExit("[錯誤] 兩個來源軌對到同一個目標軌，頭像會互相蓋掉")

    root = ET.Element("xmeml", version="4")
    out_seq = sub(root, "sequence", id="sequence-avatars")
    sub(out_seq, "name", bmp_only(f"頭像_{seq.findtext('name') or xml_path.stem}"))
    sub(out_seq, "duration", max((p["spans"][-1][1] for p in plan if p["spans"]), default=0))
    rate_el(out_seq, *fps)
    tc = sub(out_seq, "timecode")
    rate_el(tc, *fps)
    sub(tc, "string", "00:00:00:00")
    sub(tc, "frame", 0)
    sub(tc, "displayformat", "DF" if fps[1] == 1001 else "NDF")
    video = sub(sub(out_seq, "media"), "video")
    fsc = sub(sub(video, "format"), "samplecharacteristics")
    rate_el(fsc, *fps)
    sub(fsc, "width", seq_w)
    sub(fsc, "height", seq_h)
    sub(fsc, "pixelaspectratio", "square")

    by_dst = {p["dst"]: p for p in plan}
    still_len = int(STILL_DURATION_SEC * fr)
    low = by_dst[min(by_dst)]
    first = min((p["spans"][0][0] for p in plan if p["spans"]), default=0)
    anchor = None  # track number for the 00:00 anchor; must be empty at 00:00 in the original
    if first > 0:
        anchor = next((n for n in range(min(by_dst), len(vtracks) + 1)
                       if not occupied_at_zero(vtracks[n - 1])), None)
    for no in range(1, max(max(by_dst), anchor or 0) + 1):
        track = sub(video, "track")
        p = by_dst.get(no)
        if no == anchor and not p:
            p = dict(low, spans=[], filters=[])  # anchor alone on a non-avatar track
        if not p:
            continue
        fid = f"file-avatar-{no}"
        spans = ([(0, 1)] if no == anchor else []) + list(p["spans"])
        for k, (a, b) in enumerate(spans):
            is_anchor = no == anchor and k == 0
            ci = sub(track, "clipitem", id=f"avatar-{no}-{k}")
            sub(ci, "name", ANCHOR_NAME if is_anchor else bmp_only(p["image"].name))
            sub(ci, "enabled", "FALSE" if is_anchor else "TRUE")
            sub(ci, "duration", still_len)
            rate_el(ci, *fps)
            sub(ci, "start", a)
            sub(ci, "end", b)
            sub(ci, "in", 0)
            sub(ci, "out", b - a)
            sub(ci, "alphatype", "straight")
            if k:
                sub(ci, "file", id=fid)
            else:
                f = sub(ci, "file", id=fid)
                sub(f, "name", bmp_only(p["image"].name))
                sub(f, "pathurl", path_url(p["image"]))
                rate_el(f, *fps)
                sub(f, "duration", still_len)
                vsc = sub(sub(sub(f, "media"), "video"), "samplecharacteristics")
                sub(vsc, "width", p["size"][0])
                sub(vsc, "height", p["size"][1])
            for flt in p["filters"]:
                ci.append(copy.deepcopy(flt))
        if no in by_dst:
            print(f"V{p['src']} → V{no}（{p['label']}）：{p['cards']} 張字卡 → 補 {len(p['spans'])} 段頭像")

    out = Path(args.out) if args.out else xml_path.with_name(bmp_only(xml_path.stem) + "_頭像.xml")
    ET.indent(root)
    with atomic_write(out) as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
                 + ET.tostring(root, encoding="unicode") + "\n")
    print(f"→ {out}")
    if anchor:
        print("Premiere：匯入這個 XML → 開「頭像_」序列全選、複製 → 回原序列，播放頭放在 00:00、"
              f"目標軌設 V{min(by_dst)} → 貼上 → 刪掉 V{anchor} 在 00:00 那個停用的「{ANCHOR_NAME}」")
    else:
        print("Premiere：匯入這個 XML → 開「頭像_」序列全選、複製 → 回原序列，"
              f"播放頭放在 {timecode(first, fps)}（第一個頭像出現的時間）、目標軌設 V{min(by_dst)} → 貼上"
              + ("（頭像軌以上在 00:00 都有東西，所以沒放對齊物件）" if first > 0 else ""))
    if any(p["filters"] for p in plan):
        print("投影等效果 XML 帶不過來：對已放好的頭像按「複製」，選取新頭像 →「貼上屬性」勾效果")


if __name__ == "__main__":
    main()
