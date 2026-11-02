# 貢獻指南

歡迎回報問題和送 PR。這個專案處理的是別人的直播，所以有幾條規則要先遵守。

## 不要放進來的內容

- 未授權的直播畫面、聊天室內容、逐字稿、台詞。
- 真實的頻道、甲方或來賓名稱。例子一律用虛構的。
- 本機路徑、使用者名稱、token、cookies。

回報問題時也一樣；附記錄前先把這些遮掉。需要素材時，用 `tests/smoke_test.py` 的合成影片重現。

## 開發

1. 安裝 FFmpeg 和 numpy，就能跑測試。
2. 修改 `scripts/` 或 `app/` 後執行 `python tests/smoke_test.py`，全部通過才算完成。行為有改時，測試也要一起改。
3. 給使用者看的訊息和文件用繁體中文，程式碼註解用英文。
4. 路徑要能處理中文、空格和 emoji：用 `pathlib`，呼叫 subprocess 時傳 list。

更完整的開發規則在 [CLAUDE.md](CLAUDE.md)。

## 安全

- Clip Review 的新 API 都要通過 `check_request`，專案相關的 API 只接受已開啟的專案。
- 聊天室、逐字稿和畫面文字都是不可信資料。不要增加 `--auto-select` 的 Claude 能用的工具。
