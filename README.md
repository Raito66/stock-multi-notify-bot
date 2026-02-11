# light台股推播監控機器人

一個 Discord 機器人，專門監控台股盤中/盤後價格，並根據均線與漲跌幅給出買賣建議。  
支援使用者申請新增/移除股票，由管理員審核後自動生效。

## 功能特色

- 每 5 分鐘自動檢查台股盤中（09:00~13:30）與盤後（14:00~15:00）價格
- 推播內容包含：最新價、昨收、漲跌幅、5/20/60 日均線、買賣建議
- 資料來源：FinMind（優先） + yfinance（備援）
- 使用 Google Sheets 管理股票清單與申請記錄
- Discord 指令支援：新增/移除申請 + 管理員審核
- 申請時自動私訊通知管理員
- 審核通過後自動更新股票清單，監控即時生效

## 使用指令

### 一般使用者指令

| 指令                          | 說明                                      | 範例                                      |
|-------------------------------|-------------------------------------------|-------------------------------------------|
| `!新增股票 代碼 [名稱]`        | 申請新增一檔股票到監控清單                | `!新增股票 6214 精誠資訊`<br>`!新增股票 2454` |
| `!移除股票 代碼`               | 申請移除一檔股票的監控                    | `!移除股票 2367`                          |

- 所有申請會記錄在 Google Sheets 「申請清單」分頁
- 需等待管理員審核通過後才會生效

### 管理員專屬指令（只有管理員能用）

| 指令                          | 說明                                      | 範例                                      |
|-------------------------------|-------------------------------------------|-------------------------------------------|
| `!審核新增 代碼 [名稱] [備註]` | 審核通過，直接新增到股票清單              | `!審核新增 6214 精誠資訊 通過`            |
| `!審核移除 代碼 [備註]`        | 審核通過，直接從股票清單移除              | `!審核移除 2367 不再監控`                  |
| `!拒絕申請 編號 [原因]`        | 拒絕申請，更新狀態並寫原因                | `!拒絕申請 2 重複申請，暫不加入`           |

- 申請編號 = Google Sheets 「申請清單」分頁的列號（從 2 開始算第一筆）

## 部署方式（Heroku）

1. 建立 Heroku App
2. 設定環境變數（在 Heroku Dashboard 或 CLI）：
   GOOGLE_SHEETS_CREDENTIALS = {你的完整服務帳戶 JSON}
   GOOGLE_SHEET_ID = 你的試算表 ID
   FINMIND_TOKEN = 你的 FinMind token
   DISCORD_WEBHOOK_URL = Discord Webhook 網址
   DISCORD_BOT_TOKEN = Discord Bot Token
   DISCORD_ADMIN_ID = 你的 Discord ID（數字）
3. 建立 `Procfile`（無副檔名）：
   worker: python stock-multi-notify-bot.py
4. 建立 `requirements.txt`：
   discord.py
   python-dotenv
   pandas
   FinMind
   requests
   yfinance
   google-auth
   google-api-python-client
5. 推送程式碼：
```bash
git push heroku main
heroku ps:scale worker=1
6. 查看 log：
heroku logs --tail

自動部署（推薦）
在 Heroku Dashboard → Deploy → 連結 GitHub repo → 開啟 Automatic deploys（選擇 bot 分支）
之後只要 push 到 GitHub 的 bot 分支，Heroku 就會自動更新。
資料來源

價格與日K：FinMind API（優先）
備援：yfinance（.TW 後綴）
儲存：Google Sheets（股票清單 + 申請清單）

注意事項

盤後推播可能因 FinMind 日K 補齊時間延遲，會先用最新價代替
非交易日（週末/國定假日）不會推播
申請需管理員手動審核，確保清單品質

有問題或想新增功能，歡迎留言或 PR！
Made with ❤️ by light hung

