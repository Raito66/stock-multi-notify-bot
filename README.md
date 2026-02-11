# Discord 台股監控 Bot

一個每 5 分鐘自動推播台股盤中/盤後資訊的 Discord Bot，並支援使用者申請新增/移除股票。

## 功能
- 自動監控指定股票價格與均線
- 盤中/盤後/昨日收盤推播
- 交易日判斷
- 使用者可透過指令申請增刪股票（寫入 Google Sheets）

## 部署方式（Heroku）

1. Clone 專案
2. 建立 `.env` 並填入變數
3. `pip install -r requirements.txt`
4. `heroku create`
5. `heroku config:set ...` 設定所有環境變數
6. `git push heroku main`
7. `heroku ps:scale worker=1`

## 環境變數
- GOOGLE_SHEETS_CREDENTIALS
- GOOGLE_SHEET_ID
- FINMIND_TOKEN
- DISCORD_WEBHOOK_URL
- DISCORD_BOT_TOKEN