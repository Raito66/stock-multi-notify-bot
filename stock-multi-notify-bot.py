import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
load_dotenv()

import json
import pandas as pd
from FinMind.data import DataLoader
import requests
import yfinance as yf
import gc

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ======================== 環境變數 ========================
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# 從 .env 讀取管理員 Discord ID
DISCORD_ADMIN_ID = os.getenv("DISCORD_ADMIN_ID")
if not DISCORD_ADMIN_ID:
    raise RuntimeError("缺少 DISCORD_ADMIN_ID 環境變數，請在 .env 裡設定")

try:
    ADMIN_ID = int(DISCORD_ADMIN_ID)
except ValueError:
    raise RuntimeError("DISCORD_ADMIN_ID 必須是數字")

if not all([GOOGLE_SHEETS_CREDENTIALS, GOOGLE_SHEET_ID, FINMIND_TOKEN, DISCORD_BOT_TOKEN]):
    raise RuntimeError("缺少必要的環境變數")

# ======================== 參數設定 ========================
STOCK_LIST_SHEET = "股票清單"          # 股票代碼與名稱清單分頁
REQUEST_SHEET = "申請清單"             # 申請記錄分頁
MONITOR_INTERVAL_MINUTES = 5           # 維持 5 分鐘

# ======================== Discord Bot 設定 ========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ======================== Google Sheets 相關函式 ========================
def get_sheets_service():
    try:
        creds_json = GOOGLE_SHEETS_CREDENTIALS
        credentials_info = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)
        print("✅ Google Sheets 連線成功")
        return service
    except Exception as e:
        print(f"⚠️ Google Sheets 連線失敗：{e}")
        return None


def get_current_stock_list(service):
    try:
        range_name = f"'{STOCK_LIST_SHEET}'!A2:B500"
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=range_name
        ).execute()
        values = result.get('values', [])
        stock_dict = {}
        for row in values:
            if len(row) >= 1:
                code = str(row[0]).strip().zfill(4)
                if len(code) == 4 and code.isdigit():
                    name = row[1].strip() if len(row) > 1 and row[1].strip() else code
                    stock_dict[code] = name
        print(f"從 '{STOCK_LIST_SHEET}' 讀到 {len(stock_dict)} 支股票")
        return stock_dict
    except Exception as e:
        print(f"讀取股票清單失敗：{e}")
        return {}


def add_stock_to_list(service, stock_id, stock_name):
    stock_id_str = str(stock_id).zfill(4)
    values = [[stock_id_str, stock_name.strip() or stock_id_str]]

    try:
        response = service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{STOCK_LIST_SHEET}'!A:B",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",  # 插入新列，而不是一直append到最底
            body={"values": values}
        ).execute()

        print(f"寫入成功：{stock_id_str} {stock_name}，回應：{response}")
        return True, "寫入成功，已插入到股票清單分頁"
    except Exception as e:
        error_msg = str(e)
        print(f"寫入失敗：{stock_id_str} {stock_name} → {error_msg}")
        return False, error_msg


def remove_stock_from_list(service, stock_id):
    try:
        stock_id_str = str(stock_id).zfill(4)
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{STOCK_LIST_SHEET}'!A2:A500"
        ).execute()
        values = result.get('values', [])
        rows_to_delete = []
        for idx, row in enumerate(values):
            if len(row) > 0 and str(row[0]).strip() == stock_id_str:
                rows_to_delete.append(idx + 2)

        if not rows_to_delete:
            print(f"{stock_id_str} 在清單中不存在，無需刪除")
            return True, "無需刪除，已存在"

        for row_idx in sorted(rows_to_delete, reverse=True):
            service.spreadsheets().batchUpdate(
                spreadsheetId=GOOGLE_SHEET_ID,
                body={
                    "requests": [{
                        "deleteDimension": {
                            "range": {
                                "sheetId": 0,
                                "dimension": "ROWS",
                                "startIndex": row_idx - 1,
                                "endIndex": row_idx
                            }
                        }
                    }]
                }
            ).execute()
            print(f"已刪除 {stock_id_str} 第 {row_idx} 列")

        print(f"{stock_id_str} 已從股票清單移除，共刪除 {len(rows_to_delete)} 筆")
        return True, f"已刪除 {len(rows_to_delete)} 筆"
    except Exception as e:
        error_msg = str(e)
        print(f"移除失敗：{error_msg}")
        return False, error_msg


def append_request_to_sheets(service, action, stock_id, stock_name="", requester="", status="待審核", note=""):
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    stock_id_str = str(stock_id).zfill(4)
    values = [[now_str, requester, action, stock_id_str, stock_name, status, note]]

    try:
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{REQUEST_SHEET}'!A:G",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        print(f"申請記錄已寫入：{action} {stock_id_str} 狀態：{status}")
        return True
    except Exception as e:
        print(f"寫入申請記錄失敗：{e}")
        return False


# ======================== 管理員通知函式 ========================
async def notify_admin(action, stock_id, stock_name, requester, note=""):
    admin = bot.get_user(ADMIN_ID)
    if admin:
        try:
            await admin.send(
                f"【新申請】\n"
                f"申請者：{requester}\n"
                f"動作：{action}\n"
                f"股票：{stock_id} {stock_name}\n"
                f"時間：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"備註：{note}\n"
                f"請到 Sheets 審核：https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit#gid=0"
            )
            print(f"已私訊通知管理員：{action} {stock_id}")
        except Exception as e:
            print(f"私訊管理員失敗：{e}")
    else:
        print("找不到管理員使用者，無法私訊通知")


# ======================== Discord 指令 ========================
@bot.event
async def on_ready():
    print(f"機器人已上線：{bot.user}")
    monitor_stocks.start()


def is_admin(ctx):
    return ctx.author.id == ADMIN_ID


@bot.command(name="新增股票")
@commands.cooldown(1, 60, commands.BucketType.user)
async def add_stock(ctx, stock_id: str, *, stock_name: str = ""):
    stock_id = stock_id.strip()
    if not (stock_id.isdigit() and len(stock_id) <= 4):
        await ctx.send("股票代碼必須是數字，最多 4 位")
        return

    requester = f"{ctx.author} ({ctx.author.id})"
    service = get_sheets_service()
    if not service:
        await ctx.send("無法連線 Google Sheets，申請失敗")
        return

    success = append_request_to_sheets(
        service,
        "新增",
        stock_id,
        stock_name.strip() or stock_id,
        requester,
        status="待審核",
        note=""
    )

    if success:
        await ctx.send(f"已收到申請：新增 **{stock_id.zfill(4)}** {stock_name}\n請等待管理員審核。")
        await notify_admin("新增", stock_id.zfill(4), stock_name, requester)
    else:
        await ctx.send("申請失敗，請稍後再試或聯絡管理員。")


@bot.command(name="移除股票")
@commands.cooldown(1, 60, commands.BucketType.user)
async def remove_stock(ctx, stock_id: str):
    stock_id = stock_id.strip()
    if not (stock_id.isdigit() and len(stock_id) <= 4):
        await ctx.send("股票代碼必須是數字，最多 4 位")
        return

    requester = f"{ctx.author} ({ctx.author.id})"
    service = get_sheets_service()
    if not service:
        await ctx.send("無法連線 Google Sheets，申請失敗")
        return

    success = append_request_to_sheets(
        service,
        "移除",
        stock_id,
        "",
        requester,
        status="待審核",
        note=""
    )

    if success:
        await ctx.send(f"已收到申請：移除 **{stock_id.zfill(4)}**\n請等待管理員審核。")
        await notify_admin("移除", stock_id.zfill(4), "", requester)
    else:
        await ctx.send("申請失敗，請稍後再試或聯絡管理員。")


@bot.command(name="審核新增")
@commands.check(is_admin)
async def approve_add(ctx, stock_id: str, *, stock_name: str = ""):
    stock_id = stock_id.strip()
    if not stock_id.isdigit():
        await ctx.send("股票代碼必須是數字")
        return

    stock_id_str = stock_id.zfill(4)
    if len(stock_id_str) != 4:
        await ctx.send("請輸入最多 4 位的股票代碼")
        return

    service = get_sheets_service()
    if not service:
        await ctx.send("無法連線 Google Sheets")
        return

    # 嘗試寫入股票清單，並取得詳細結果
    success_add, msg = add_stock_to_list(service, stock_id_str, stock_name.strip() or stock_id_str)

    if success_add:
        await ctx.send(f"**已審核通過**：新增 **{stock_id_str} {stock_name.strip() or stock_id_str}** 到股票清單\n{msg}")
    else:
        await ctx.send(f"新增 **{stock_id_str}** 到股票清單**失敗**：{msg}\n請檢查 Google Sheets 權限、分頁名稱，或查看 Heroku log")
        return

    # 如果寫入成功，再更新申請清單狀態
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{REQUEST_SHEET}'!A:G"
        ).execute()
        values = result.get('values', [])

        latest_row_num = None
        latest_time = None

        for idx, row in enumerate(values):
            if len(row) < 6:
                continue

            action = str(row[2]).strip() if row[2] else ""
            code = str(row[3]).strip().zfill(4) if row[3] is not None else ""
            status_raw = row[5] if len(row) > 5 else None
            status = str(status_raw).strip() if status_raw is not None else ""

            if action == "新增" and code == stock_id_str and status == "待審核":
                request_time = str(row[0]).strip() if row[0] else ""
                if latest_time is None or (request_time and request_time > latest_time):
                    latest_time = request_time
                    latest_row_num = idx + 2

        if latest_row_num is not None:
            update_range = f"{REQUEST_SHEET}!F{latest_row_num}:G{latest_row_num}"
            update_values = [["已通過", "管理員已審核通過"]]

            service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=update_range,
                valueInputOption="USER_ENTERED",
                body={"values": update_values}
            ).execute()

            await ctx.send(f"已更新申請時間 {latest_time} 的記錄為「已通過」")
        else:
            await ctx.send("已新增到清單，但未找到對應的待審核申請，狀態未更新，請手動檢查")

    except Exception as e:
        await ctx.send(f"更新申請狀態失敗：{str(e)}\n但股票已加入清單（若成功）")


@bot.command(name="審核移除")
@commands.check(is_admin)
async def approve_remove(ctx, stock_id: str):
    stock_id = stock_id.strip()
    if not stock_id.isdigit():
        await ctx.send("股票代碼必須是數字")
        return

    stock_id_str = stock_id.zfill(4)
    if len(stock_id_str) != 4:
        await ctx.send("請輸入最多 4 位的股票代碼")
        return

    service = get_sheets_service()
    if not service:
        await ctx.send("無法連線 Google Sheets")
        return

    success_remove, msg = remove_stock_from_list(service, stock_id_str)

    if success_remove:
        await ctx.send(f"已審核通過：移除 **{stock_id_str}** 從股票清單\n{msg}")
    else:
        await ctx.send(f"移除 **{stock_id_str}** 失敗：{msg}")
        return

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{REQUEST_SHEET}'!A:G"
        ).execute()
        values = result.get('values', [])

        latest_row_num = None
        latest_time = None

        for idx, row in enumerate(values):
            if len(row) < 6:
                continue

            action = str(row[2]).strip() if row[2] else ""
            code = str(row[3]).strip().zfill(4) if row[3] is not None else ""
            status_raw = row[5] if len(row) > 5 else None
            status = str(status_raw).strip() if status_raw is not None else ""

            if action == "移除" and code == stock_id_str and status == "待審核":
                request_time = str(row[0]).strip() if row[0] else ""
                if latest_time is None or (request_time and request_time > latest_time):
                    latest_time = request_time
                    latest_row_num = idx + 2

        if latest_row_num is not None:
            update_range = f"{REQUEST_SHEET}!F{latest_row_num}:G{latest_row_num}"
            update_values = [["已通過", "管理員已審核通過"]]

            service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=update_range,
                valueInputOption="USER_ENTERED",
                body={"values": update_values}
            ).execute()

            await ctx.send(f"已更新申請時間 {latest_time} 的記錄為「已通過」")
        else:
            await ctx.send("已移除股票，但未找到對應待審核申請，狀態未更新")

    except Exception as e:
        await ctx.send(f"更新申請狀態失敗：{str(e)}\n但股票已移除（若成功）")


@bot.command(name="拒絕申請")
@commands.check(is_admin)
async def reject_request(ctx, request_row: int, *, reason: str = "未說明原因"):
    if request_row < 2:
        await ctx.send("申請編號從 2 開始（第 2 列為第一筆申請）")
        return

    service = get_sheets_service()
    if not service:
        await ctx.send("無法連線 Google Sheets")
        return

    try:
        update_range = f"{REQUEST_SHEET}!F{request_row}:G{request_row}"
        update_values = [["已拒絕", reason]]
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=update_range,
            valueInputOption="USER_ENTERED",
            body={"values": update_values}
        ).execute()
        await ctx.send(f"已拒絕申請編號 {request_row}，原因：{reason}")
    except Exception as e:
        await ctx.send(f"拒絕申請失敗：{e}")


# ======================== 工具函式 ========================
def write_log(msg):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open("error.log", "a", encoding="utf-8") as f:
        f.write(f"{now_str} {msg}\n")
    print(msg)


def send_discord_push(message: str):
    if not DISCORD_WEBHOOK_URL:
        write_log("未設定 DISCORD_WEBHOOK_URL，無法推播 Discord。")
        return
    data = {"content": message}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
        if resp.status_code != 204:
            write_log(f"Discord 推播失敗，狀態碼：{resp.status_code}")
        else:
            write_log("Discord 推播成功")
    except Exception as e:
        write_log(f"Discord 推播失敗：{e}")


def is_trading_day(dl: DataLoader, check_date: str, is_after_close: bool) -> bool:
    symbol_for_check = "2330"
    try:
        yesterday = (datetime.strptime(check_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        df = dl.taiwan_stock_daily(symbol_for_check, start_date=yesterday, end_date=yesterday)
        if not df.empty:
            del df
            gc.collect()
            return True
        del df
        gc.collect()
        return False
    except Exception as e:
        write_log(f"交易日檢查錯誤：{e}")
        return False


def get_latest_available_price(dl, stock_id: str):
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    tw_symbol = f"{stock_id}.TW"

    try:
        df = dl.taiwan_stock_daily(stock_id, start_date=today, end_date=today)
        if not df.empty:
            latest = df.iloc[0]
            price = float(latest["close"])
            time_str = today
            del df
            gc.collect()
            return {
                "price": price,
                "time": time_str,
                "source": "today_daily_finmind",
                "is_latest": True,
                "finmind_success": True
            }
    except Exception as e:
        write_log(f"FinMind 當天日K失敗：{e}")

    try:
        ticker = yf.Ticker(tw_symbol)
        hist = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            latest = hist.iloc[-1]
            price = float(latest["Close"])
            time_str = latest.name.strftime("%Y-%m-%d %H:%M:%S")
            del hist
            gc.collect()
            return {
                "price": price,
                "time": time_str,
                "source": "today_yfinance",
                "is_latest": True,
                "finmind_success": False
            }
    except Exception as e:
        write_log(f"yfinance 即時資料失敗：{e}")

    try:
        hist_daily = ticker.history(period="5d")
        if not hist_daily.empty:
            latest = hist_daily.iloc[-1]
            price = float(latest["Close"])
            date_str = latest.name.strftime("%Y-%m-%d")
            del hist_daily
            gc.collect()
            return {
                "price": price,
                "time": date_str,
                "source": "previous_yfinance",
                "is_latest": False,
                "finmind_success": False
            }
    except Exception as e:
        write_log(f"yfinance 最近日K失敗：{e}")

    return None


def get_today_close(dl, stock_id: str, date_str: str) -> Optional[float]:
    try:
        df = dl.taiwan_stock_daily(stock_id, start_date=date_str, end_date=date_str)
        if not df.empty:
            price = float(df.iloc[0]["close"])
            del df
            gc.collect()
            return price
        del df
        gc.collect()
        return None
    except Exception as e:
        write_log(f"取得 {date_str} 收盤價失敗：{e}")
        return None


def get_stock_data(dl, stock_id: str) -> Optional[Dict]:
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    is_after_close = now.hour > 13 or (now.hour == 13 and now.minute >= 30)

    instant = get_latest_available_price(dl, stock_id)
    if not instant:
        return None

    yesterday_close = get_today_close(dl, stock_id, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    if yesterday_close is None:
        yesterday_close = instant["price"]

    result = {
        "stock_id": stock_id,
        "latest_price": instant["price"],
        "latest_time": instant["time"],
        "yesterday_close": yesterday_close,
        "date": today,
        "is_after_close": is_after_close,
        "source": instant["source"],
        "is_latest": instant["is_latest"],
        "finmind_success": instant.get("finmind_success", False)
    }

    if is_after_close:
        close_price = get_today_close(dl, stock_id, today)
        if close_price:
            result["close_price"] = close_price
        else:
            result["close_price"] = instant["price"]
        result["close_time"] = instant["time"]

    return result


def calculate_ma(prices, window):
    if len(prices) < window:
        return None
    s = pd.Series(prices)
    result = s.rolling(window).mean().iloc[-1]
    del s
    return result


def get_intraday_advice(latest, ma5, ma20, ma60, pct):
    if not (ma5 and ma20):
        return "均線資料不夠，先等等看比較好"

    diff_ma5 = (latest - ma5) / ma5 * 100 if ma5 else 0
    diff_ma20 = (latest - ma20) / ma20 * 100 if ma20 else 0

    if latest > ma5 and latest > ma20:
        if diff_ma5 <= 2.8 and 3.0 <= pct <= 6.0:
            return "剛突破均線 + 今天力道很強，建議可以全部買進（但設好停損點）"
        elif diff_ma5 > 7.5 or (diff_ma5 > 6.0 and pct > 4.5):
            return "現在明顯過熱 + 漲幅很大，建議全部賣出鎖利，或至少先賣 70%~100%"
        elif pct > 5.0:
            return "今天漲很多，建議先賣 50%~80% 鎖住部分利潤，剩下的看明天"
        elif diff_ma5 > 4.5:
            return "股價已經漲不少，現在偏貴，建議先觀望，或最多用 10%~20% 的資金試試看"
        elif 1.5 <= pct < 3.5:
            return "今天有往上力道，建議先用 25%~45% 的資金分批買進"
        elif abs(pct) < 1.2:
            if pct > 0:
                return "小漲站上均線，建議先用 10%~25% 的資金試試看"
            else:
                return "站上均線但今天沒力道，建議先觀望，不要急著買"
        else:
            return "漲太快了，建議先不要追，最多用 15%~30% 的資金小量進場"

    elif latest < ma5 and latest < ma20:
        if pct < -5.0:
            return "今天跌很多 + 跌破均線，建議全部賣出止損，或至少先賣 70%~100%"
        elif pct < -2.5:
            return "跌破均線 + 跌幅明顯，建議先賣 40%~70% 降低風險"
        else:
            return "股價在均線下面，建議暫時不要買，等反彈再看"

    elif abs(pct) > 7.0:
        if pct > 7.0:
            return "今天漲超兇，建議先賣 60%~90% 鎖住大部分利潤"
        else:
            return "今天跌超兇，建議先賣 60%~90% 避險"

    else:
        return "現在情況不明，先觀望比較安全，等明天再說"


def get_after_close_summary(latest, ma5, ma20, ma60, change):
    if ma5 and latest > ma5 and ma20 and latest > ma20:
        return "建議明天可以買進，今天收盤價比平均價高"
    elif ma5 and latest < ma5 and ma20 and latest < ma20:
        return "建議明天不要買，今天收盤價比平均價低"
    elif abs(change) < 1:
        return "今天沒什麼變化，明天再觀察"
    else:
        return "今天價格有變動，明天再看情況決定要不要買"


# ======================== 定時監控任務 ========================
@tasks.loop(minutes=MONITOR_INTERVAL_MINUTES)
async def monitor_stocks():
    now = datetime.now(timezone(timedelta(hours=8)))
    now_str = now.strftime("%Y年%m月%d日 %H時%M分%S秒")
    write_log(f"開始定時監控：{now_str}")

    service = get_sheets_service()
    if not service:
        return

    STOCK_NAME_MAP = get_current_stock_list(service)
    STOCK_LIST = list(STOCK_NAME_MAP.keys())
    del service
    gc.collect()

    dl = DataLoader()
    try:
        dl.login_by_token(FINMIND_TOKEN)
    except Exception as e:
        write_log(f"FinMind 登入失敗：{e}")
        return

    is_after_close = now.hour > 13 or (now.hour == 13 and now.minute >= 30)

    if not is_trading_day(dl, now.strftime("%Y-%m-%d"), is_after_close):
        if is_after_close:
            write_log("盤後模式：視為交易日")
        else:
            write_log("今天非交易日，跳過監控")
            return

    write_log("通過交易日檢查，開始處理股票資料...")

    is_yesterday_push = (now.hour == 13 and 31 <= now.minute < 59)
    is_today_push = (now.hour >= 14)

    for stock_id in STOCK_LIST:
        try:
            stock_name = STOCK_NAME_MAP.get(stock_id, stock_id)
            stock = get_stock_data(dl, stock_id)
            if not stock:
                write_log(f"{stock_id} 無法取得資料，跳過")
                continue

            df = dl.taiwan_stock_daily(
                stock_id,
                start_date=(now - timedelta(days=20)).strftime("%Y-%m-%d"),
                end_date=now.strftime("%Y-%m-%d")
            )

            closes = df["close"].tolist() if not df.empty else []

            ma5 = calculate_ma(closes, 5)
            ma20 = calculate_ma(closes, 20)
            ma60 = calculate_ma(closes, 60)

            del df
            del closes
            gc.collect()

            ma5_str = f"{ma5:.2f}" if ma5 is not None else "無資料"
            ma20_str = f"{ma20:.2f}" if ma20 is not None else "無資料"
            ma60_str = f"{ma60:.2f}" if ma60 is not None else "無資料"

            latest = stock["latest_price"]
            yesterday_close = stock["yesterday_close"]
            change = latest - yesterday_close
            pct = change / yesterday_close * 100 if yesterday_close != 0 else 0

            if stock.get("finmind_success", False):
                if stock["source"] == "today_tick_finmind":
                    source_note = f"（{stock['latest_time']}）"
                elif stock["source"] == "today_daily_finmind":
                    source_note = f"（{stock['latest_time']} 當天收盤）"
                else:
                    source_note = f"（{stock['latest_time']}）"
            else:
                if stock["source"] == "today_yfinance":
                    source_note = f"（{stock['latest_time']}）（yfinance 備援）"
                else:
                    source_note = f"（{stock['latest_time']} 收盤）（yfinance 備援）"

            footnote = "※ 資料來源：FinMind（yfinance 為備援來源）"

            if is_yesterday_push:
                msg = [
                    f"---",
                    f"【{stock_id} {stock_name} 昨日收盤價 {now.strftime('%Y年%m月%d日')}】",
                    f"時間：{now_str}",
                    "━━━━━━━━━━━━━━",
                    f"昨收：{yesterday_close:.2f} 元",
                    f"5日均線：{ma5_str}",
                    f"20日均線：{ma20_str}",
                    f"60日均線：{ma60_str}",
                    f"建議：{get_intraday_advice(yesterday_close, ma5, ma20, ma60, 0)}",
                    "※ 資料來源：FinMind"
                ]
                send_discord_push("\n".join(msg))
                write_log(f"{stock_id} 推播昨日收盤價完成")
                gc.collect()
                continue

            if is_today_push and stock["is_after_close"]:
                close_price_for_sheet = get_today_close(dl, stock_id, stock["date"])
                if close_price_for_sheet is None:
                    write_log(f"{stock_id} 盤後寫入：FinMind 當天日K尚未有資料，使用最新價代替")
                    close_price = stock["latest_price"]
                    close_note = f"{stock['latest_time']} （盤後暫用最新價，等待日K補齊）"
                else:
                    close_price = close_price_for_sheet
                    close_note = f"{stock['latest_time']} （日K正式收盤）"

                msg = [
                    f"---",
                    f"【{stock_id} {stock_name} 價格監控 {now.strftime('%Y年%m月%d日')}】",
                    f"時間：{now_str}",
                    "━━━━━━━━━━━━━━",
                    f"最新價：{latest:.2f} 元{source_note}",
                    f"昨收：{yesterday_close:.2f} 元",
                    f"漲跌：{change:+.2f}（{pct:+.2f}%）",
                    f"5日均線：{ma5_str}",
                    f"20日均線：{ma20_str}",
                    f"60日均線：{ma60_str}",
                    f"今日收盤：{close_price:.2f} 元{close_note}",
                    f"行情摘要：{get_after_close_summary(latest, ma5, ma20, ma60, change)}",
                    footnote
                ]

                send_discord_push("\n".join(msg))
                write_log(f"{stock_id} 推播盤後資訊完成")
                gc.collect()
                continue

            # 盤中推播
            msg = [
                f"---",
                f"【{stock_id} {stock_name} 盤中監控 {now.strftime('%Y年%m月%d日')}】",
                f"時間：{now_str}",
                "━━━━━━━━━━━━━━",
                f"最新價：{latest:.2f} 元{source_note}",
                f"昨收：{yesterday_close:.2f} 元",
                f"漲跌：{change:+.2f}（{pct:+.2f}%）",
                f"5日均線：{ma5_str}",
                f"20日均線：{ma20_str}",
                f"60日均線：{ma60_str}",
                f"建議：{get_intraday_advice(latest, ma5, ma20, ma60, pct)}",
                footnote
            ]

            send_discord_push("\n".join(msg))
            write_log(f"{stock_id} 盤中推播完成")

        except Exception as e:
            write_log(f"{stock_id} 處理錯誤：{e}")
        finally:
            gc.collect()
            time.sleep(0.3)
            gc.collect()

    del STOCK_NAME_MAP
    del STOCK_LIST
    gc.collect()
    write_log("本次監控循環結束，已強制回收記憶體")


# 啟動 Bot
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)