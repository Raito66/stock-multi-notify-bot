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

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ======================== 環境變數 ========================
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not all([GOOGLE_SHEETS_CREDENTIALS, GOOGLE_SHEET_ID, FINMIND_TOKEN, DISCORD_BOT_TOKEN]):
    raise RuntimeError("缺少必要的環境變數")

# ======================== 參數設定 ========================
SHEET_NAME = "股票清單"          # 正式清單分頁名稱（用來顯示 log）
SHEET_INDEX = 0                   # 分頁索引（0 = 第一個分頁，改成你的實際索引）
REQUEST_SHEET_NAME = "申請清單"
MONITOR_INTERVAL_MINUTES = 5

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


def get_stock_info_from_sheets(service, spreadsheet_id, sheet_index=SHEET_INDEX):
    try:
        # 使用分頁索引（0 = 第一個分頁）避免中文名稱問題
        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_name = sheet_metadata['sheets'][sheet_index]['properties']['title']
        range_name = f"'{sheet_name}'!A2:B"

        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name
        ).execute()

        values = result.get('values', [])
        stock_dict = {}
        for row in values:
            if len(row) >= 1:
                code = row[0].strip()
                if len(code) == 4 and code.isdigit():
                    name = row[1].strip() if len(row) > 1 and row[1].strip() else code
                    stock_dict[code] = name

        if not stock_dict:
            print("未讀到股票資料，使用預設清單")
            return {
                "2330": "台積電", "6770": "力積電", "3481": "群創",
                "2337": "旺宏", "2344": "華邦電", "2409": "友達", "2367": "燿華"
            }

        print(f"從 Google Sheets 分頁 '{sheet_name}' 讀到 {len(stock_dict)} 支股票：{list(stock_dict.keys())}")
        return stock_dict

    except Exception as e:
        print(f"讀取股票資訊失敗：{e}")
        return {
            "2330": "台積電", "6770": "力積電", "3481": "群創",
            "2337": "旺宏", "2344": "華邦電", "2409": "友達", "2367": "燿華"
        }


def append_request_to_sheets(service, action, stock_id, stock_name="", requester=""):
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    values = [[now_str, requester, action, stock_id, stock_name, "待審核", ""]]

    try:
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{REQUEST_SHEET_NAME}!A:G",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        print(f"申請已寫入：{action} {stock_id}")
        return True
    except Exception as e:
        print(f"寫入申請失敗：{e}")
        return False


# ======================== Discord 指令 ========================
@bot.event
async def on_ready():
    print(f"機器人已上線：{bot.user}")
    monitor_stocks.start()


@bot.command(name="新增股票")
@commands.cooldown(1, 60, commands.BucketType.user)
async def add_stock(ctx, stock_id: str, *, stock_name: str = ""):
    stock_id = stock_id.strip()
    if not (stock_id.isdigit() and len(stock_id) == 4):
        await ctx.send("股票代碼必須是 4 位數字，例如 2330")
        return

    requester = f"{ctx.author} ({ctx.author.id})"
    success = append_request_to_sheets(
        get_sheets_service(),
        "新增",
        stock_id,
        stock_name.strip(),
        requester
    )

    if success:
        await ctx.send(f"已收到申請：新增 **{stock_id}** {stock_name}\n請等待管理員審核。")
    else:
        await ctx.send("申請失敗，請稍後再試或聯絡管理員。")


@bot.command(name="移除股票")
@commands.cooldown(1, 60, commands.BucketType.user)
async def remove_stock(ctx, stock_id: str):
    stock_id = stock_id.strip()
    if not (stock_id.isdigit() and len(stock_id) == 4):
        await ctx.send("股票代碼必須是 4 位數字")
        return

    requester = f"{ctx.author} ({ctx.author.id})"
    success = append_request_to_sheets(
        get_sheets_service(),
        "移除",
        stock_id,
        "",
        requester
    )

    if success:
        await ctx.send(f"已收到申請：移除 **{stock_id}**\n請等待管理員審核。")
    else:
        await ctx.send("申請失敗，請稍後再試或聯絡管理員。")


# ======================== 工具函式 ========================
def write_log(msg):
    now_str = datetime.now().strftime('%Y年%m月%d日 %H時%M分%S秒')
    with open("error.log", "a", encoding="utf-8") as f:
        f.write(f"{now_str} {msg}\n")
    print(f"{now_str} {msg}")


def send_discord_push(message: str):
    if not DISCORD_WEBHOOK_URL:
        write_log("未設定 DISCORD_WEBHOOK_URL，無法推播 Discord。")
        return
    data = {"content": message}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
        if resp.status_code != 204:
            write_log(f"Discord 推播失敗，狀態碼：{resp.status_code}，回應：{resp.text}")
        else:
            write_log("Discord 推播成功")
    except Exception as e:
        write_log(f"Discord 推播失敗：{e}")


def is_trading_day(dl: DataLoader, check_date: str, is_after_close: bool) -> bool:
    symbol_for_check = "2330"
    try:
        if is_after_close:
            df = dl.taiwan_stock_daily(symbol_for_check, start_date=check_date, end_date=check_date)
            if not df.empty:
                write_log(f"盤後檢查：{check_date} 有日K資料，視為交易日")
                return True
            else:
                write_log(f"盤後檢查：{check_date} 無日K資料，視為非交易日")
                return False
        else:
            yesterday = (datetime.strptime(check_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            df = dl.taiwan_stock_daily(symbol_for_check, start_date=yesterday, end_date=yesterday)
            if not df.empty:
                write_log(f"盤中檢查：{yesterday} 有交易資料，今天很可能為交易日")
                return True
            else:
                write_log(f"盤中檢查：{yesterday} 無交易資料，今天很可能休市")
                return False
    except Exception as e:
        write_log(f"交易日檢查發生錯誤：{e}，預設為非交易日")
        return False


def get_latest_available_price(dl, stock_id: str):
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    tw_symbol = f"{stock_id}.TW"

    try:
        df = dl.get_data(dataset="TaiwanStockPrice", data_id=stock_id, start_date=today)
        if df is not None and not df.empty and 'close' in df.columns:
            latest = df.iloc[-1]
            time_str = latest["date"]
            if "Time" in df.columns and pd.notna(latest.get("Time", None)):
                time_str = f"{latest['date']} {latest['Time']}"
            price = float(latest["close"])
            write_log(f"{stock_id} 取得當天最新分鐘價（FinMind）：{price:.2f} @ {time_str}")
            return {
                "price": price,
                "time": time_str,
                "source": "today_tick_finmind",
                "is_latest": True,
                "finmind_success": True
            }
    except Exception as e:
        write_log(f"{stock_id} FinMind 當天分鐘價失敗：{e}")

    try:
        df_day = dl.taiwan_stock_daily(stock_id, start_date=today, end_date=today)
        if not df_day.empty:
            price = float(df_day.iloc[0]["close"])
            write_log(f"{stock_id} 取得當天日收盤價（FinMind）：{price:.2f}")
            return {
                "price": price,
                "time": f"{today} 收盤",
                "source": "today_daily_finmind",
                "is_latest": True,
                "finmind_success": True
            }
    except Exception as e:
        write_log(f"{stock_id} FinMind 當天日收盤價失敗：{e}")

    write_log(f"{stock_id} FinMind 今天完全無資料 → 改用 yfinance 備援")
    try:
        ticker = yf.Ticker(tw_symbol)
        hist = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            latest = hist.iloc[-1]
            price = float(latest["Close"])
            time_str = latest.name.strftime("%Y-%m-%d %H:%M:%S")
            write_log(f"{stock_id} yfinance 取得最新分鐘價：{price:.2f} @ {time_str}")
            return {
                "price": price,
                "time": time_str,
                "source": "today_yfinance",
                "is_latest": True,
                "finmind_success": False
            }

        hist_daily = ticker.history(period="5d")
        if not hist_daily.empty:
            latest = hist_daily.iloc[-1]
            price = float(latest["Close"])
            date_str = latest.name.strftime("%Y-%m-%d")
            write_log(f"{stock_id} yfinance 取得最近日收盤價：{price:.2f} ({date_str})")
            return {
                "price": price,
                "time": date_str,
                "source": "previous_yfinance",
                "is_latest": False,
                "finmind_success": False
            }
    except Exception as e:
        write_log(f"{stock_id} yfinance 備援也失敗：{e}")

    write_log(f"{stock_id} FinMind 與 yfinance 都無法取得任何價格")
    return None


def get_today_close(dl, stock_id: str, date_str: str) -> Optional[float]:
    try:
        df = dl.taiwan_stock_daily(stock_id, start_date=date_str, end_date=date_str)
        if not df.empty:
            return float(df.iloc[0]["close"])
        return None
    except Exception as e:
        write_log(f"{stock_id} 取得 {date_str} 收盤價失敗：{e}")
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
    return pd.Series(prices).rolling(window).mean().iloc[-1]


def save_to_sheets(service, stock_id, stock_name, date, price, ma5, ma20, ma60, timestamp):
    if not service:
        return False
    try:
        values = [[stock_id, stock_name, date, price, ma5, ma20, ma60, timestamp]]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{SHEET_NAME}!A2",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        write_log(f"{stock_id} 寫入 Sheets 成功：{date} - {price:.2f}")
        return True
    except Exception as e:
        write_log(f"{stock_id} 寫入 Sheets 失敗：{e}")
        return False


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

    STOCK_NAME_MAP = get_stock_info_from_sheets(service, GOOGLE_SHEET_ID)
    STOCK_LIST = list(STOCK_NAME_MAP.keys())

    dl = DataLoader()
    try:
        dl.login_by_token(FINMIND_TOKEN)
    except Exception as e:
        write_log(f"FinMind 登入失敗：{e}")
        return

    is_after_close = now.hour > 13 or (now.hour == 13 and now.minute >= 30)

    if not is_trading_day(dl, now.strftime("%Y-%m-%d"), is_after_close):
        write_log(f"今天非交易日，跳過本次監控")
        return

    write_log("通過交易日檢查，開始處理股票資料...")

    is_yesterday_push = (now.hour == 13 and 31 <= now.minute < 59)
    is_today_push = (now.hour >= 14)

    for stock_id in STOCK_LIST:
        stock_name = STOCK_NAME_MAP.get(stock_id, stock_id)
        stock = get_stock_data(dl, stock_id)
        if not stock:
            write_log(f"{stock_id} 無法取得資料，跳過")
            continue

        df = dl.taiwan_stock_daily(
            stock_id,
            start_date=(now - timedelta(days=61)).strftime("%Y-%m-%d"),
            end_date=now.strftime("%Y-%m-%d")
        )
        closes = df["close"].tolist() if not df.empty else []

        ma5 = calculate_ma(closes, 5)
        ma20 = calculate_ma(closes, 20)
        ma60 = calculate_ma(closes, 60)

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
            continue

        if is_today_push and stock["is_after_close"]:
            close_price_for_sheet = get_today_close(dl, stock_id, stock["date"])
            if close_price_for_sheet is None:
                write_log(f"{stock_id} 盤後寫入：FinMind 當天日K尚未有資料，跳過寫入")
                close_price = stock["latest_price"]
                close_note = f"{stock['latest_time']} （當前最新價）"
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

            if close_price_for_sheet is not None:
                save_to_sheets(
                    service, stock_id, stock_name, stock["date"],
                    close_price_for_sheet, ma5, ma20, ma60, now_str
                )

            send_discord_push("\n".join(msg))
            write_log(f"{stock_id} 推播盤後資訊完成")
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


# 啟動 Bot
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)