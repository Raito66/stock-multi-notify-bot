import os
from dotenv import load_dotenv
load_dotenv()
import json
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
from FinMind.data import DataLoader
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gc

# ======================== 環境變數 ========================
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")

if not all([GOOGLE_SHEETS_CREDENTIALS, GOOGLE_SHEET_ID, FINMIND_TOKEN]):
    raise RuntimeError("缺少必要的環境變數")

# ======================== 參數設定 ========================
STOCK_LIST_SHEET = "股票清單"         # 股票清單分頁（第一分頁）
HISTORY_SHEET_NAME = "股票清單"       # 歷史資料寫入分頁（與清單同分頁）
BATCH_DAYS = 20                       # 每次補最近 20 天（可加大，但注意記憶體）
SLEEP_BETWEEN_STOCKS = 120            # 每支股票處理完休息 120 秒
SLEEP_BETWEEN_WRITES = 8              # 每寫 8 筆休息一次（防 Google API 限流）

# ======================== 工具函式 ========================
def write_log(msg):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open("error.log", "a", encoding="utf-8") as f:
        f.write(f"{now_str} {msg}\n")
    print(msg)

def get_sheets_service():
    try:
        creds_json = GOOGLE_SHEETS_CREDENTIALS
        credentials_info = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)
        write_log("✅ Google Sheets 連線成功")
        return service
    except Exception as e:
        write_log(f"⚠️ Google Sheets 連線失敗：{e}")
        return None

# 動態讀取股票清單（從第一分頁 A2:B）
def load_stock_list(service):
    global STOCK_LIST, STOCK_NAME_MAP
    try:
        range_name = f"'{STOCK_LIST_SHEET}'!A2:B"
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=range_name
        ).execute()
        values = result.get('values', [])
        STOCK_NAME_MAP = {}
        for row in values:
            if len(row) >= 1:
                code = row[0].strip()
                if len(code) == 4 and code.isdigit():
                    name = row[1].strip() if len(row) > 1 else code
                    STOCK_NAME_MAP[code] = name
        STOCK_LIST = list(STOCK_NAME_MAP.keys())
        write_log(f"從 '{STOCK_LIST_SHEET}' 讀到 {len(STOCK_LIST)} 支股票：{STOCK_LIST}")
    except Exception as e:
        write_log(f"讀取股票清單失敗：{e}")
        STOCK_LIST = ["2330", "6770", "3481", "2337", "2344", "2409", "2367"]
        STOCK_NAME_MAP = {
            "2330": "台積電", "6770": "力積電", "3481": "群創",
            "2337": "旺宏", "2344": "華邦電", "2409": "友達", "2367": "燿華"
        }

def load_history_from_sheets(service, stock_id=None):
    if not service:
        return []
    try:
        range_name = f"'{HISTORY_SHEET_NAME}'!A2:H"
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=range_name
        ).execute()
        values = result.get("values", [])
        history = []
        for row in values:
            if len(row) >= 4 and (stock_id is None or row[0] == stock_id):
                try:
                    price = float(row[3]) if row[3] else None
                except:
                    price = None
                history.append({
                    "date": row[2],
                    "price": price,
                    "ma5": row[4] if len(row) > 4 else None,
                    "ma20": row[5] if len(row) > 5 else None,
                    "ma60": row[6] if len(row) > 6 else None,
                    "timestamp": row[7] if len(row) > 7 else row[2]
                })
        return history
    except Exception as e:
        write_log(f"讀取 Sheets 失敗：{e}")
        return []

def update_row_in_sheets(service, stock_id, date, stock_name, price, ma5, ma20, ma60, timestamp):
    try:
        range_name = f"'{HISTORY_SHEET_NAME}'!A2:H"
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=range_name
        ).execute()
        values = result.get("values", [])
        for idx, row in enumerate(values):
            if len(row) > 2 and row[0] == stock_id and row[2] == date:
                update_range = f"'{HISTORY_SHEET_NAME}'!A{idx+2}:H{idx+2}"
                update_values = [[stock_id, stock_name, date, price, ma5, ma20, ma60, timestamp]]
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range=update_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": update_values}
                ).execute()
                write_log(f"{stock_id} 覆蓋成功：{date}")
                return True
        # 沒找到就新增（8 欄）
        values = [[stock_id, stock_name, date, price, ma5, ma20, ma60, timestamp]]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{HISTORY_SHEET_NAME}'!A2",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        write_log(f"{stock_id} 新增成功：{date}")
        return True
    except Exception as e:
        write_log(f"{stock_id} 更新/新增失敗：{e}")
        return False

def calculate_ma(prices, window):
    if len(prices) < window:
        return None
    return pd.Series(prices).rolling(window).mean().iloc[-1]

# ======================== 主補齊函式 ========================
def fill_missing_history(service, dl):
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    end_date = now.strftime("%Y-%m-%d")

    # 動態讀取目前股票清單
    load_stock_list(service)
    if not STOCK_LIST:
        write_log("無法讀取股票清單，結束補齊")
        return

    for stock_id in STOCK_LIST:
        stock_name = STOCK_NAME_MAP.get(stock_id, stock_id)
        write_log(f"開始處理 {stock_id} ({stock_name})")
        history = load_history_from_sheets(service, stock_id)
        history_map = {h["date"]: h for h in history}
        start_date = (now - timedelta(days=BATCH_DAYS)).strftime("%Y-%m-%d")
        write_log(f"{stock_id} 下載範圍：{start_date} ~ {end_date}")
        df = dl.taiwan_stock_daily(stock_id, start_date=start_date, end_date=end_date)
        if df.empty:
            write_log(f"{stock_id} 最近 {BATCH_DAYS} 天無資料，跳過")
            continue
        dates = df["date"].tolist()
        closes = df["close"].tolist()
        updated = 0
        for i, date in enumerate(dates):
            price = closes[i]
            ma5 = calculate_ma(closes[:i+1], 5) if i+1 >= 5 else None
            ma20 = calculate_ma(closes[:i+1], 20) if i+1 >= 20 else None
            ma60 = calculate_ma(closes[:i+1], 60) if i+1 >= 60 else None
            timestamp = f"{date} 00:00:00"
            exist = history_map.get(date)
            need_update = True
            if exist:
                if all([
                    exist.get("price") not in (None, '', 'None'),
                    exist.get("ma5") not in (None, '', '無資料'),
                    exist.get("ma20") not in (None, '', '無資料'),
                    exist.get("ma60") not in (None, '', '無資料')
                ]):
                    need_update = False
            if need_update:
                success = update_row_in_sheets(
                    service, stock_id, date, stock_name, price, ma5, ma20, ma60, timestamp
                )
                if success:
                    updated += 1
            if (i + 1) % SLEEP_BETWEEN_WRITES == 0:
                time.sleep(5)
        write_log(f"{stock_id} 本次完成：更新/補齊 {updated} 筆（最近 {BATCH_DAYS} 天）")
        del df, dates, closes
        gc.collect()
        time.sleep(SLEEP_BETWEEN_STOCKS)

# ======================== 主程式 ========================
def main():
    write_log("=== 開始補齊歷史收盤價與均線（8 欄格式） ===")
    service = get_sheets_service()
    if not service:
        write_log("無法連線 Google Sheets，結束執行")
        return
    dl = DataLoader()
    dl.login_by_token(FINMIND_TOKEN)
    fill_missing_history(service, dl)
    write_log("=== 補齊流程結束 ===")

if __name__ == "__main__":
    main()