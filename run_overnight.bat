@echo off
rem 整晚跑完分析＋Claude 挑選（不出片）。複製這個檔案，改下面三行，再用 Windows「工作排程器」設定時間執行。
rem 早上打開 start_clip_review.bat，從「最近的專案」進去審核即可。
chcp 65001 >nul
cd /d "%~dp0scripts"

set VIDEO=D:\直播\20260101\直播.mp4
set URL=https://www.youtube.com/watch?v=XXXXXXXXXXX
set CONFIG=D:\直播\config.json

python pipeline.py --video "%VIDEO%" --url "%URL%" --config "%CONFIG%" --auto-select
