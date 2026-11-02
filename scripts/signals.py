"""Merge chat / keyword / audio / speech signals and pick candidate highlight moments.

Usage: python signals.py --workdir WORK [--config config.json] [--duration SECONDS] [--top N]

Outputs:
  signals.csv              per-second signal table (for reference / plotting)
  candidates.json          ranked candidate moments with scores
  candidates_context.md    each candidate with transcript around it + chat sample,
                           written for Claude to read and judge.
"""
import argparse
import math

import numpy as np

from common import (fmt_ts, load_chat, load_config, load_transcript, probe,
                    save_json, work_paths)


ALIGNED_SHIFT_SEC = 30    # within this, the shift is just people reacting; real streams show about ±15 s
MIN_SHIFT_STRENGTH = 3.5


def robust_z(x):
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    if mad < 1e-6:
        sd = np.std(x)
        mad = sd if sd > 1e-6 else 1.0
    return (x - med) / mad


def tail_z(x, q=99, at=6.0):
    """Score sparse event signals (mostly zero) so the top (100-q)% of seconds reach `at`.
    robust_z would divide by a near-zero MAD and saturate on every small blip."""
    med = np.median(x)
    hi = np.percentile(x, q) - med
    if hi < 1e-6:
        return robust_z(x)
    return (x - med) / hi * at


def rolling_sum(x, w):
    if w <= 1:
        return x.copy()
    k = np.ones(w)
    return np.convolve(x, k, mode="same")


def rolling_median(x, w):
    """Cheap local baseline: median over blocks, then interpolate."""
    n = len(x)
    if n == 0:
        return x
    centers, vals = [], []
    for s in range(0, n, max(1, w // 4)):
        a, b = max(0, s - w // 2), min(n, s + w // 2)
        centers.append(s)
        vals.append(np.median(x[a:b]))
    return np.interp(np.arange(n), centers, vals)


def load_series(path, cols, n):
    """Per-second columns from a `t,...` CSV as arrays of length n (zeros where missing)."""
    out = {c: np.zeros(n) for c in cols}
    if not path.exists():
        return out, False
    data = np.genfromtxt(path, delimiter=",", names=True)
    data = np.atleast_1d(data)
    if not len(data):
        return out, False
    idx = data["t"].astype(int)
    ok = (idx >= 0) & (idx < n)
    for c in cols:
        out[c][idx[ok]] = data[c][ok]
    return out, True


def estimate_chat_shift(chat_cnt, loud, max_shift=300, smooth=10):
    """How many seconds chat activity lags the stream's loudness, by cross-correlation.
    -> (shift seconds, strength), or None when there is too little to go on. An aligned stream
    still shows a small shift; strength is the peak in robust standard deviations."""
    n = len(chat_cnt)
    if n < 4 * max_shift or chat_cnt.sum() < 30 or not loud.any():
        return None
    k = np.ones(smooth) / smooth
    c, a = np.convolve(chat_cnt, k, "same"), np.convolve(loud, k, "same")
    if c.std() < 1e-9 or a.std() < 1e-9:
        return None
    c, a = (c - c.mean()) / c.std(), (a - a.mean()) / a.std()
    shifts = np.arange(-max_shift, max_shift + 1)
    r = np.array([(c[max(0, s):n + min(0, s)] * a[max(0, -s):n - max(0, s)]).mean() for s in shifts])
    med = np.median(r)
    spread = np.median(np.abs(r - med)) * 1.4826
    return int(shifts[r.argmax()]), float((r.max() - med) / spread) if spread > 1e-9 else 0.0


def offset_hint(chat, chat_cnt, loud, off, duration):
    """A warning about chat_offset_sec, or None. Only ever a suggestion: it never changes the offset."""
    if chat:
        late = max(m["t"] for m in chat) + off - duration
        if late > 60:
            return f"聊天室比影片多了 {late:.0f} 秒，影片可能比存檔晚開或被剪過，請檢查 chat_offset_sec"
    est = estimate_chat_shift(chat_cnt, loud)
    if est and abs(est[0]) > ALIGNED_SHIFT_SEC and est[1] >= MIN_SHIFT_STRENGTH:
        return (f"聊天室反應和音量差了約 {est[0]:+d} 秒（強度 {est[1]:.1f}），時間可能沒對齊。"
                f"請用候選點確認，需要的話把 chat_offset_sec 改成 {off - est[0]:.0f}")
    return None


def kw_score(text, table):
    if not text:
        return 0.0
    low = text.lower()
    return sum(wt for kw, wt in table.items() if kw.lower() in low)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--video", help="用來讀取總長度（沒有音量檔時）")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--top", type=int)
    args = ap.parse_args()

    cfg = load_config(args.config)
    scfg = cfg["signals"]
    W = int(scfg["window_sec"])
    wts = scfg["weights"]
    paths = work_paths(args.workdir)

    chat = load_chat(paths["chat"])
    segs = load_transcript(paths["transcript_json"])
    audio = None
    if paths["audio"].exists():
        audio = np.loadtxt(paths["audio"], delimiter=",", skiprows=1, ndmin=2)

    dur = args.duration
    if not dur and audio is not None and len(audio):
        dur = audio[-1, 0] + 1
    if not dur and args.video:
        dur = probe(args.video)["duration"]
    if not dur:
        dur = max([m["t"] for m in chat] + [s["end"] for s in segs] + [0]) + 1
    n = int(math.ceil(dur))
    vis, has_vis = load_series(paths["visual"], ("cut", "flash", "fade"), n)
    voc, has_voc = load_series(paths["vocal"], ("scream", "laugh"), n)
    print(f"直播長度 {fmt_ts(n)}；留言 {len(chat)} 則；逐字稿 {len(segs)} 段；音量資料 {'有' if audio is not None else '無'}；"
          f"畫面 {'有' if has_vis else '無'}；人聲反應 {'有' if has_voc else '無'}")

    off = float(cfg.get("chat_offset_sec", 0))
    lag = float(scfg.get("chat_lag_sec", 8))  # chat reacts after the event; pull it back
    chat_cnt = np.zeros(n)
    chat_kw = np.zeros(n)
    sc = np.zeros(n)
    for m in chat:
        t = int(m["t"] + off - lag)
        if 0 <= t < n:
            chat_cnt[t] += 1
            chat_kw[t] += kw_score(m.get("text", ""), cfg["chat_keywords"])
            if m.get("type") in ("superchat", "sticker"):
                sc[t] += 1

    speech = np.zeros(n)
    for s in segs:
        t = int(s["start"])
        if 0 <= t < n:
            speech[t] += kw_score(s["text"], cfg["speech_keywords"])

    loud = np.zeros(n)
    if audio is not None and len(audio):
        db = np.full(n, np.nan)
        idx = audio[:, 0].astype(int)
        ok = idx < n
        db[idx[ok]] = audio[ok, 1]
        db = np.nan_to_num(db, nan=np.nanmedian(db))
        # loudness relative to the local 5-minute baseline, positives only
        loud = np.clip(db - rolling_median(db, 300), 0, None)

    hint = offset_hint(chat, chat_cnt, loud, off, n)
    if hint:
        print(f"[提示] {hint}")

    # screen change: strongest of scene cut / flash / fade, each relative to the whole stream
    visual = np.zeros(n)
    if has_vis:
        visual = np.clip(np.max([tail_z(vis[c]) for c in ("cut", "flash", "fade")], axis=0), 0, None)

    comps = {}
    total = np.zeros(n)
    short = max(3, W // 2)
    for name, arr, w in (("chat", chat_cnt, wts["chat"]), ("keywords", chat_kw, wts["keywords"]),
                         ("audio", loud, wts["audio"]), ("superchat", sc, wts["superchat"]),
                         ("speech", speech, wts["speech"]), ("visual", visual, wts.get("visual", 0)),
                         ("scream", voc["scream"], wts.get("scream", 0)),
                         ("laugh", voc["laugh"], wts.get("laugh", 0))):
        if not arr.any():
            comps[name] = np.zeros(n)
            continue
        # momentary signals (sound / picture) use a short mean; counts use a window sum
        if name in ("audio", "visual", "scream", "laugh"):
            win = rolling_sum(arr, short) / short
        else:
            win = rolling_sum(arr, W)
        z = np.clip(tail_z(win) if name in ("visual", "scream", "laugh") else robust_z(win), -2, 8)
        comps[name] = z
        total += w * z

    with open(paths["signals"], "w", encoding="utf-8") as f:
        f.write("t,chat,chat_kw,audio_rel_db,superchat,speech_kw,visual,scream,laugh,score\n")
        for t in range(n):
            f.write(f"{t},{int(chat_cnt[t])},{chat_kw[t]:.1f},{loud[t]:.1f},{int(sc[t])},{speech[t]:.1f},"
                    f"{visual[t]:.2f},{voc['scream'][t]:.2f},{voc['laugh'][t]:.3f},{total[t]:.2f}\n")

    # --- peak picking with minimum separation ---
    top = args.top or max(int(scfg["min_candidates"]), int(round(n / 3600 * scfg["candidates_per_hour"])))
    gap = int(scfg["min_gap_sec"])
    order = np.argsort(-total)
    taken = []
    for t in order:
        if total[t] <= 0 or len(taken) >= top:
            break
        if all(abs(int(t) - p) >= gap for p in taken):
            taken.append(int(t))
    taken.sort()

    cands = []
    for i, t in enumerate(taken, 1):
        a, b = max(0, t - W), min(n, t + W)
        cands.append({
            "id": f"c{i:02d}",
            "peak": t,
            "peak_ts": fmt_ts(t),
            "score": round(float(total[t]), 2),
            "breakdown": {k: round(float(v[max(0, t - 15):t + 10].max()), 2) for k, v in comps.items()},
            "chat_in_window": int(chat_cnt[a:b].sum()),
            "superchats_in_window": int(sc[a:b].sum()),
        })
    rank = sorted(cands, key=lambda c: -c["score"])
    for r, c in enumerate(rank, 1):
        c["rank"] = r
    save_json({"duration": n, "window_sec": W, "chat_offset_sec": off, "chat_offset_hint": hint, "candidates": cands},
              paths["candidates"])

    write_context(paths["context"], cands, segs, chat, off, n, hint)
    print(f"挑出 {len(cands)} 個候選點 → {paths['context']}")


def write_context(path, cands, segs, chat, off, n, hint=None):
    lines = ["# 候選精華點（依時間排序）",
             "",
             *([f"**注意：{hint}**", ""] if hint else []),
             "說明：peak 是綜合訊號最高的秒數，聊天室訊號已往前平移約 chat_lag_sec 秒以對齊事件，",
             "但實際事件仍可能再早幾秒。逐字稿給 peak 前 90 秒到後 45 秒；",
             "聊天室列的是原始時間（比事件晚）。訊號數字是 peak 附近各訊號的最高標準分數（>2 算明顯）。",
             ""]
    for c in cands:
        t = c["peak"]
        lines.append(f"## {c['id']}　{c['peak_ts']}　分數 {c['score']}（排名 {c['rank']}）")
        bd = "，".join(f"{k} {v}" for k, v in c["breakdown"].items() if v > 0.5)
        lines.append(f"訊號：{bd or '弱'}；附近留言 {c['chat_in_window']} 則，超級留言 {c['superchats_in_window']}")
        lines.append("")
        lines.append("逐字稿：")
        lo, hi = t - 90, t + 45
        got = False
        for s in segs:
            if s["end"] >= lo and s["start"] <= hi:
                mark = " ◀" if s["start"] <= t <= s["end"] + 3 else ""
                lines.append(f"  [{fmt_ts(s['start'])}] {s['text']}{mark}")
                got = True
        if not got:
            lines.append("  （無語音／尚未轉錄）")
        lines.append("")
        near = [m for m in chat if t - 10 <= m["t"] + off <= t + 30 and m.get("text")]
        if near:
            # superchats first, then an even spread of the rest
            scs = [m for m in near if m.get("type") in ("superchat", "sticker")][:5]
            rest = [m for m in near if m not in scs]
            step = max(1, len(rest) // (18 - len(scs)))
            sample = sorted(scs + rest[::step][: 18 - len(scs)], key=lambda m: m["t"])
            lines.append("聊天室：")
            for m in sample:
                tag = f"[SC {m['amount']}] " if m.get("type") in ("superchat", "sticker") else ""
                lines.append(f"  [{fmt_ts(m['t'] + off)}] {tag}{m['text']}")
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
