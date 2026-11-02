# TODO

依優先順序排列。標「已確認」的是讀程式碼確認過的現況；其他是改進想法，還沒驗證效果。

## P2：提升挑選和字幕品質


## 之後可以考慮

- **從語音指示讀說話者**：有些頻道畫面上有語音軟體的頭像，說話時會亮。用來驗證 pyannote 時發現它分得很清楚，而且只要解碼選定段落。可以做成依版面設定的選項，準確度可能高於 pyannote，但只適用有這種畫面的直播。

## P3：Premiere 工作流程

- [x] **驗證並記錄直式序列匯入**：合成素材的 MP4 版和可調整版都匯入 Premiere 2024，使用者沒發現問題。
- [x] **驗證可調整直式序列**：`--vertical editable` 用合成素材和真實 60fps 直播加版型 PNG 匯入 Premiere 2024，和 MP4 對照畫面一致。只測過這一支，預設仍是 MP4。
- [ ] **驗證 XML 帶入片尾卡和轉場字卡**：`premiere.cards` 已實作，還沒匯入 Premiere 確認。
- [x] **匯入字幕更省事**：XML 帶不了字幕，序列 00:00 的標記會寫出要匯入的 SRT 檔名。

## P4：讓其他人更容易使用（公開專案）

- [ ] **README 加上截圖或短片**：使用虛構素材，不要出現真實頻道內容。
- [ ] **實際測試 `fetch_chat.py --url`**：目前唯一尚未在真實環境驗證的主要步驟。
- [ ] **版型校正精靈**：在 Clip Review 內拖框設定 `crop_presets`，不用手動讀格線圖。
- [ ] **GitHub Actions**：在 Windows 上自動跑 `tests/smoke_test.py`。
- [ ] **補測試**：`avatar_tracks.py` 目前沒有測試（已確認）；Clip Review 的 analyze、render、preview API 也沒有。
- [ ] **鎖定相依版本**：`requirements.txt` 目前沒有指定版本。
- [ ] **跨平台**：`.bat` 和 `os.startfile` 只支援 Windows，補上 macOS／Linux 的啟動方式。
- [ ] **英文 README 與其他語言設定**：把關鍵字和簡轉繁獨立成語言設定檔。
- [ ] **貢獻指南與 issue 範本**：提醒回報問題時不要附上未授權的直播內容或個人路徑。
