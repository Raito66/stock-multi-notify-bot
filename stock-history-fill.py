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
HISTORY_SHEET_NAME = "股票清單"       # 歷史資料寫入分頁（目前與清單同分頁）
BATCH_DAYS = 30                       # 每次補最近 30 天（可調整）
MAX_HISTORY_PER_STOCK = 365           # 每支股票最多保留 365 筆（約一年）
SLEEP_BETWEEN_STOCKS = 90             # 每支股票處理完休息 90 秒（降低記憶體壓力）
SLEEP_BETWEEN_WRITES = 5              # 每寫 5 筆休息一次（防 API 限流）

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
    try:
        range_name = f"'{STOCK_LIST_SHEET}'!A2:B"
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=range_name
        ).execute()
        values = result.get('values', [])
        stock_dict = {}
        for row in values:
            if len(row) >= 1:
                # 強制轉字串 + 補齊 4 位 + 去空白
                code = str(row[0]).strip().zfill(4)
                if len(code) == 4 and code.isdigit():
                    name = row[1].strip() if len(row) > 1 and row[1].strip() else code
                    stock_dict[code] = name
        write_log(f"從 '{STOCK_LIST_SHEET}' 讀到 {len(stock_dict)} 支股票：{list(stock_dict.keys())}")
        return stock_dict
    except Exception as e:
        write_log(f"讀取股票清單失敗：{e}")
        return {}

# 只讀取指定股票的歷史資料（大幅省記憶體）
def load_history_for_stock(service, stock_id):
    try:
        stock_id_str = str(stock_id).zfill(4)
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{HISTORY_SHEET_NAME}'!A2:H"
        ).execute()
        values = result.get("values", [])
        history = []
        for row in values:
            if len(row) >= 4 and str(row[0]).strip() == stock_id_str:
                try:
                    price = float(row[3]) if row[3] and row[3] != '' else None
                except:
                    price = None
                history.append({
                    "row_index": values.index(row) + 2,
                    "date": row[2],
                    "price": price,
                    "ma5": row[4] if len(row) > 4 else None,
                    "ma20": row[5] if len(row) > 5 else None,
                    "ma60": row[6] if len(row) > 6 else None,
                    "timestamp": row[7] if len(row) > 7 else row[2]
                })
        write_log(f"{stock_id_str} 讀到 {len(history)} 筆歷史資料")
        return history
    except Exception as e:
        write_log(f"讀取 {stock_id} 歷史失敗：{e}")
        return []

# 更新或新增單一列（8 欄對應標題）
def update_or_append_row(service, stock_id, date, stock_name, price, ma5, ma20, ma60, timestamp):
    try:
        stock_id_str = str(stock_id).zfill(4)
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{HISTORY_SHEET_NAME}'!A2:H"
        ).execute()
        values = result.get("values", [])
        for idx, row in enumerate(values):
            if len(row) > 2 and str(row[0]).strip() == stock_id_str and row[2] == date:
                update_range = f"'{HISTORY_SHEET_NAME}'!A{idx+2}:H{idx+2}"
                update_values = [[stock_id_str, stock_name, date, price, ma5, ma20, ma60, timestamp]]
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range=update_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": update_values}
                ).execute()
                write_log(f"{stock_id_str} 覆蓋成功：{date}")
                return True
        # 沒找到就新增（8 欄）
        values = [[stock_id_str, stock_name, date, price, ma5, ma20, ma60, timestamp]]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{HISTORY_SHEET_NAME}'!A2",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        write_log(f"{stock_id_str} 新增成功：{date}")
        return True
    except Exception as e:
        write_log(f"{stock_id} 更新/新增失敗：{e}")
        return False

def calculate_ma(prices, window):
    if len(prices) < window:
        return None
    return pd.Series(prices).rolling(window).mean().iloc[-1]

# 清理舊資料，只保留最新 limit 筆（預設 365）
def trim_history_to_limit(service, stock_id, limit=365):
    try:
        stock_id_str = str(stock_id).zfill(4)
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{HISTORY_SHEET_NAME}'!A2:H"
        ).execute()
        values = result.get("values", [])
        stock_rows = [(i+2, row) for i, row in enumerate(values) if len(row) >= 4 and str(row[0]).strip() == stock_id_str]
        if len(stock_rows) <= limit:
            write_log(f"{stock_id_str} 目前 {len(stock_rows)} 筆，無需清理（上限 {limit}）")
            return
        # 保留最新的 limit 筆
        keep_rows = stock_rows[-limit:]
        # 保留非該股票的資料 + 該股票最新 limit 筆
        new_values = [row for _, row in keep_rows] + \
                     [row for row in values if len(row) < 4 or str(row[0]).strip() != stock_id_str]
        # 清空範圍後重新寫入
        service.spreadsheets().values().clear(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{HISTORY_SHEET_NAME}'!A2:H",
            body={}
        ).execute()
        if new_values:
            service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=f"'{HISTORY_SHEET_NAME}'!A2",
                valueInputOption="USER_ENTERED",
                body={"values": new_values}
            ).execute()
        write_log(f"{stock_id_str} 清理完成，保留最新 {len(keep_rows)} 筆（上限 {limit}）")
    except Exception as e:
        write_log(f"{stock_id} 清理失敗：{e}")

# ======================== 主補齊函式 ========================
def fill_missing_history(service, dl):
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    end_date = now.strftime("%Y-%m-%d")

    # 動態讀取目前股票清單
    stock_dict = load_stock_list(service)
    if not stock_dict:
        write_log("無法讀取股票清單，結束補齊")
        return

    for stock_id, stock_name in stock_dict.items():
        write_log(f"開始處理 {stock_id} ({stock_name})")
        history = load_history_for_stock(service, stock_id)
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
                success = update_or_append_row(
                    service, stock_id, date, stock_name, price, ma5, ma20, ma60, timestamp
                )
                if success:
                    updated += 1
            if (i + 1) % SLEEP_BETWEEN_WRITES == 0:
                time.sleep(5)
        write_log(f"{stock_id} 本次完成：更新/補齊 {updated} 筆（最近 {BATCH_DAYS} 天）")
        # 處理完一支股票後，立即清理舊資料，只保留最新 365 筆
        trim_history_to_limit(service, stock_id, limit=MAX_HISTORY_PER_STOCK)
        del df, dates, closes
        gc.collect()
        time.sleep(SLEEP_BETWEEN_STOCKS)

# ======================== 主程式 ========================
def main():
    write_log("=== 開始補齊歷史收盤價與均線（8 欄格式，保留最多 365 筆） ===")
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