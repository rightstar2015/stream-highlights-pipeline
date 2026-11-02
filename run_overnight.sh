#!/bin/sh
# 整晚跑完分析＋Claude 挑選（不出片）。複製這個檔案，改下面三行，再用 cron 或 launchd 設定時間執行。
# 早上執行 start_clip_review.sh，從「最近的專案」進去審核即可。
cd "$(dirname "$0")/scripts" || exit 1

VIDEO="$HOME/直播/20260101/直播.mp4"
URL="https://www.youtube.com/watch?v=XXXXXXXXXXX"
CONFIG="$HOME/直播/config.json"

python3 pipeline.py --video "$VIDEO" --url "$URL" --config "$CONFIG" --auto-select
