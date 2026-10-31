"""Download and normalise a YouTube live-chat replay -> chat.jsonl.

Usage:
  python fetch_chat.py --url https://www.youtube.com/watch?v=XXXX --workdir WORK
  python fetch_chat.py --file existing.live_chat.json --workdir WORK

Each output line: {"t": seconds_from_stream_start, "author": str, "text": str,
                   "type": "text"|"superchat"|"member"|"sticker", "amount": str}
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import need_tool, work_paths


def runs_to_text(runs):
    parts = []
    for r in runs or []:
        if "text" in r:
            parts.append(r["text"])
        elif "emoji" in r:
            em = r["emoji"]
            if em.get("isCustomEmoji"):
                sc = (em.get("shortcuts") or [""])[0]
                parts.append(sc)
            else:
                parts.append(em.get("emojiId", ""))
    return "".join(parts)


def parse_item(item):
    for key, typ in (("liveChatTextMessageRenderer", "text"),
                     ("liveChatPaidMessageRenderer", "superchat"),
                     ("liveChatPaidStickerRenderer", "sticker"),
                     ("liveChatMembershipItemRenderer", "member")):
        if key in item:
            r = item[key]
            text = runs_to_text(r.get("message", {}).get("runs"))
            if typ == "member" and not text:
                text = runs_to_text(r.get("headerSubtext", {}).get("runs"))
            amount = r.get("purchaseAmountText", {}).get("simpleText", "")
            author = r.get("authorName", {}).get("simpleText", "")
            return {"author": author, "text": text, "type": typ, "amount": amount}
    return None


def parse_live_chat(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            replay = obj.get("replayChatItemAction", {})
            offset = replay.get("videoOffsetTimeMsec")
            if offset is None:
                continue
            for act in replay.get("actions", []):
                item = act.get("addChatItemAction", {}).get("item")
                if not item:
                    continue
                msg = parse_item(item)
                if msg:
                    msg["t"] = round(int(offset) / 1000.0, 2)
                    out.append(msg)
    out.sort(key=lambda m: m["t"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--file")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--cookies-from-browser", help="會員限定存檔時使用，如 chrome / edge")
    args = ap.parse_args()
    paths = work_paths(args.workdir)

    src = args.file
    if not src:
        if not args.url:
            sys.exit("請提供 --url 或 --file")
        need_tool("yt-dlp")
        tmpl = str(paths["dir"] / "chat_raw")
        cmd = ["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
               "-o", tmpl, args.url]
        if args.cookies_from_browser:
            cmd[1:1] = ["--cookies-from-browser", args.cookies_from_browser]
        print("下載聊天室重播（長直播可能要幾分鐘）...")
        subprocess.run(cmd, check=True)
        cands = sorted(paths["dir"].glob("chat_raw*.live_chat.json"))
        if not cands:
            sys.exit("[錯誤] 沒抓到聊天室檔案。可能該存檔沒有聊天室重播，或需要 --cookies-from-browser。")
        src = cands[-1]

    msgs = parse_live_chat(src)
    with open(paths["chat"], "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    sc = sum(1 for m in msgs if m["type"] in ("superchat", "sticker"))
    last = msgs[-1]["t"] if msgs else 0
    print(f"完成：{len(msgs)} 則留言（超級留言/貼圖 {sc} 則），最後一則在 {last/60:.1f} 分 → {paths['chat']}")


if __name__ == "__main__":
    main()
