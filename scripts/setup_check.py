"""Check that everything the skill needs is installed, and print how to fix what isn't."""
import importlib
import shutil
import sys

import common  # noqa: F401  (UTF-8 console)

ok = True


def check(label, good, fix):
    global ok
    print(f"{'✅' if good else '❌'} {label}" + ("" if good else f"\n    → {fix}"))
    ok = ok and good


check("Python 3.9+", sys.version_info >= (3, 9), "安裝 Python 3.11：https://www.python.org/downloads/")
check("ffmpeg", shutil.which("ffmpeg") is not None,
      "Windows：winget install Gyan.FFmpeg　/　macOS：brew install ffmpeg")
check("ffprobe", shutil.which("ffprobe") is not None, "隨 ffmpeg 一起安裝")
check("yt-dlp（抓聊天室）", shutil.which("yt-dlp") is not None, "pip install -U yt-dlp")
for mod, pipname, label in (("numpy", "numpy", "numpy"),
                            ("faster_whisper", "faster-whisper", "faster-whisper（轉錄）"),
                            ("opencc", "opencc-python-reimplemented", "OpenCC（簡轉繁）"),
                            ("jieba", "jieba", "jieba（字幕不把詞切開）")):
    try:
        importlib.import_module(mod)
        good = True
    except Exception:
        good = False
    check(label, good, f"pip install {pipname}")

try:
    import torch
    import demucs  # noqa: F401
    cuda = torch.cuda.is_available()
    print(f"{'✅' if cuda else '⚠️ '} 人聲分離（torch {torch.__version__} + demucs）" +
          ("" if cuda else "：torch 不是 CUDA 版，分離會很慢"))
except Exception:
    print("⚠️  人聲分離（選用，refine_transcript.py / transcribe.py --separate 需要）：\n"
          "    → pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128\n"
          "    → pip install --no-deps demucs && pip install dora-search einops julius openunmix lameenc pyyaml tqdm")

try:
    import ctranslate2
    n = ctranslate2.get_cuda_device_count()
    print(f"{'✅' if n else '⚠️ '} NVIDIA GPU：{n} 張" +
          ("" if n else "（沒有也能跑，但 4 小時直播用 CPU 轉錄可能要好幾個小時）"))
    if n:
        print("    若轉錄時出現 cublas / cudnn 錯誤：pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
              "（DLL 路徑 transcribe.py 會自動加入）")
        print("    模型下載卡住時：先設環境變數 HF_HUB_DISABLE_XET=1 再執行")
except Exception:
    pass

from pipeline import find_claude  # noqa: E402

claude = find_claude()
print(f"{'✅' if claude else '⚠️ '} Claude Code（pipeline.py --auto-select／Clip Review 的「讓 Claude 挑選」）" +
      (f"：{claude}" if claude else "\n    → 安裝 Claude Code 並登入：https://claude.com/claude-code（不用也能分析、手動勾選）"))
try:
    import tkinter  # noqa: F401
    print("✅ tkinter（Clip Review 的「選擇…」檔案視窗）")
except Exception:
    print("⚠️  tkinter：Clip Review 的「選擇…」按鈕不能用，請直接貼上路徑（或重裝 Python 時勾選 tcl/tk）")

print("\n全部就緒！" if ok else "\n請先安裝上面標 ❌ 的項目。")
