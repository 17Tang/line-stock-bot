import asyncio
import datetime
import hashlib
import hmac
import html
import http.server
import json
import os
import threading
import httpx
import pandas as pd
import yfinance as yf
import twstock  # ⚡ 台灣股市官方即時與中文名稱核心庫
import pytz

# ==========================================
# ⚙️ 核心設定區
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# ==========================================
# 📊 數據下載與關鍵價計算邏輯
# ==========================================
def calculate_stock_prices(stock_id):
    tw_tz = pytz.timezone("Asia/Taipei")
    now_tw = datetime.datetime.now(tw_tz)
    
    # 判斷是否為台股與大盤
    is_tw_stock = len(stock_id) >= 4 and stock_id.isdigit()
    is_tw_index = stock_id.upper() in ["^TWII", "^TWOII"]

    # 1. 統一轉換成 yfinance 歷史日K需要的 Ticker ID
    if is_tw_stock:
        ticker_id = f"{stock_id}.TW"
    else:
        ticker_id = stock_id.upper()

    # ⚡ 2. 歷史日K線下載 (專門用來計算前日、今日、周、月的關鍵價)
    try:
        # 為了保證能抓到昨天的完整日K，多給一點緩衝天數
        end_date = now_tw.date() + datetime.timedelta(days=1)
        start_date = now_tw.date() - datetime.timedelta(days=365)
        df_daily = yf.download(ticker_id, start=start_date, end=end_date, progress=False)
        
        # 若台股上市找不到，自動切換至上櫃 (.TWO)
        if is_tw_stock and df_daily.empty:
            ticker_id = f"{stock_id}.TWO"
            df_daily = yf.download(ticker_id, start=start_date, end=end_date, progress=False)
    except Exception:
        return None

    if df_daily.empty or len(df_daily) < 3:
        return None

    # 清理日K多層索引欄位
    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)

    # 子夜空值防禦機制（若當天剛換日遇到未開盤的空 K 棒，自動往前退一格）
    import numpy as np
    if pd.isna(df_daily.iloc[-1]["Close"]) or df_daily.iloc[-1]["Volume"] == 0 or np.isnan(df_daily.iloc[-1]["Close"]):
        df_daily = df_daily.iloc[:-1]

    # ⚡⚡⚡ 【三、台股 / 大盤改用 twstock 官方即時 API 交叉抓取】 ⚡⚡⚡
    
    if is_tw_stock or is_tw_index:
        try:
            # 轉換 twstock 專用的代號名稱 (大盤上市為 'tse_^TWII'，上櫃為 'otc_^TWOII')
            if ticker_id == "^TWII":
                rt_id = "tse_^TWII"
            elif ticker_id == "^TWOII":
                rt_id = "otc_^TWOII"
            elif ticker_id.endswith(".TWO"):
                rt_id = f"otc_{stock_id}.tw"
            else:
                rt_id = f"tse_{stock_id}.tw"
                
            rt_data = twstock.realtime.get(rt_id)
            
            if rt_data and rt_data.get('success'):
                info = rt_data['info']
                realtime_info = rt_data['realtime']
                
                # A. 提取 100% 真實即時現價 (若盤中為最新撮合價，若盤後為收盤價)
                # 優先抓 latest_trade_price，抓不到(例如開盤前)就抓昨日收盤
                current_price = realtime_info.get('latest_trade_price')
                if current_price is None or current_price == '-' or np.isnan(float(current_price)):
                    current_price = float(realtime_info.get('open', df_daily.iloc[-1]["Close"]))
                else:
                    current_price = float(current_price)
                
                # B. 提取昨日官方收盤價以精準計算漲跌
                yesterday_close = float(df_daily.iloc[-2]["Close"])
                
                # C. 提取證交所官方精準撮合時間 (格式: 2026-06-03 13:30:00)
                # twstock 的 time 欄位已經自動幫我們處理好台股收盤或盤中的即時時間了！
                time_str = info.get('time', '')
                if time_str:
                    # 格式轉換成標準格式
                    dt_parsed = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                    quote_time = dt_parsed.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    quote_time = f"{df_daily.index[-1].strftime('%Y-%m-%d')} 13:30:00"
            else:
                raise Exception("twstock 抓取失敗，回退至 yfinance 備用機制")
                
        except Exception:
            # 備用機制：若 twstock 網路超時，回退至 yfinance
            current_price = float(df_daily.iloc[-1]["Close"])
            yesterday_close = float(df_daily.iloc[-2]["Close"])
            quote_time = f"{df_daily.index[-1].strftime('%Y-%m-%d')} 13:30:00"
            
        # 關鍵價日K對照：此時日K的最後一筆(iloc[-1]) 就是今日(或最新交易日)，倒數第二筆(iloc[-2])就是前一日
        t_day = df_daily.iloc[-1]
        p_day = df_daily.iloc[-2]
        
    else:
        # ─── 美股標準處理邏輯 ───
        t_day = df_daily.iloc[-1]
        p_day = df_daily.iloc[-2]
        current_price = float(t_day["Close"])
        yesterday_close = float(p_day["Close"])
        
        price_date_str = df_daily.index[-1].strftime("%Y-%m-%d")
        try:
            ticker_data = yf.Ticker(ticker_id)
            last_time_utc = ticker_data.fast_info.get("last_volume_timestamp")
            if last_time_utc:
                quote_time = datetime.datetime.fromtimestamp(last_time_utc, tz=tw_tz).strftime("%Y-%m-%d %H:%M:%S")
            else:
                quote_time = f"{price_date_str} 16:00:00"
        except Exception:
            quote_time = f"{price_date_str} 16:00:00"

    # ⚡ 漲跌點數與百分比計算
    change_points = current_price - yesterday_close
    change_percent = (change_points / yesterday_close) * 100
    
    if change_points > 0:
        change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
    elif change_points < 0:
        change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
    else:
        change_str = f"─ 0.00 (0.00%)"

    # 🔍 關鍵價公式計算 (歷史日K為基底)
    t_h, t_l = float(t_day["High"]), float(t_day["Low"])
    p_h, p_l = float(p_day["High"]), float(p_day["Low"])

    t_res = t_h + (t_h - t_l) * 0.382
    t_key = (t_h + t_l) / 2
    t_sup = t_l - (t_h - t_l) * 0.382

    p_res = p_h + (p_h - p_l) * 0.382
    p_key = (p_h + p_l) / 2
    p_sup = p_l - (p_h - p_l) * 0.382

    df_weekly = df_daily.resample("W-FRI").agg({"High": "max", "Low": "min"})
    w_key = float((df_weekly.iloc[-1]["High"] + df_weekly.iloc[-1]["Low"]) / 2)

    df_monthly = df_daily.resample("ME").agg({"High": "max", "Low": "min"})
    m_key = float((df_monthly.iloc[-1]["High"] + df_monthly.iloc[-1]["Low"]) / 2)

    # 獲取官方中文名稱
    stock_name = ""
    if is_tw_stock:
        try:
            tw_info = twstock.codes.get(stock_id)
            if tw_info:
                stock_name = tw_info.name
        except Exception:
            pass
    elif ticker_id.startswith("^TWII"):
        stock_name = "上市加權指數"
    elif ticker_id.startswith("^TWOII"):
        stock_name = "櫃買指數"

    if not stock_name:
        try:
            ticker_obj = yf.Ticker(ticker_id)
            stock_name = ticker_obj.info.get("shortName", stock_id)
        except Exception:
            stock_name = stock_id

    display_name = f"{stock_id.upper()} {stock_name}"

    return {
        "ticker_id": display_name,
        "current": current_price,
        "change_str": change_str,
        "quote_time": quote_time,
        "t_res": t_res, "t_key": t_key, "t_sup": t_sup,
        "p_res": p_res, "p_key": p_key, "p_sup": p_sup,
        "w_key": w_key, "m_key": m_key
    }

# ==========================================
# 🤖 LINE Webhook 伺服器接收端
# ==========================================
class LineWebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"LINE Bot Webhook Server is running perfectly!")

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        signature = self.headers.get('X-Line-Signature', '')
        if not verify_signature(post_data, signature):
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        try:
            body = json.loads(post_data.decode('utf-8'))
            for event in body.get('events', []):
                if event.get('type') == 'message' and event['message'].get('type') == 'text':
                    reply_token = event['replyToken']
                    user_text = event['message']['text'].strip()
                    threading.Thread(target=process_and_reply_line, args=(reply_token, user_text)).start()
        except Exception as e:
            print(f"解析錯誤: {e}")

def verify_signature(body, signature):
    if not LINE_SECRET: return False
    hash = hmac.new(LINE_SECRET.encode('utf-8'), body, hashlib.sha256).digest()
    import base64
    expected_signature = base64.b64encode(hash).decode('utf-8')
    return hmac.compare_digest(expected_signature, signature)

# ==========================================
# ✉️ LINE 訊息回覆傳送邏輯
# ==========================================
def process_and_reply_line(reply_token, user_text):
    if user_text == "開始" or user_text.lower() == "hello":
        send_line_reply(reply_token, "👋 歡迎使用關鍵價看盤助手！\n\n💡 請在股號前加一個『#』即可查詢。\n👉 例如輸入：`#2330` 或 `#^TWII`")
        return

    if not user_text.startswith("#"):
        return

    stock_id = user_text[1:].strip()
    if not stock_id:
        return

    try:
        p = calculate_stock_prices(stock_id)
        if p is None:
            send_line_reply(reply_token, f"❌ 找不到股票代號 '{stock_id}' 的資料。")
            return

        report_text = (
            f"{p['ticker_id']}\n"
            f"{p['current']:.2f} {p['change_str']}\n"
            f"{p['quote_time']}\n"
            f"━━━━━━━━━━━━━\n"
            f"今日關鍵價\n"
            f"空方防守價：{p['t_res']:.2f}\n"
            f"關鍵價：{p['t_key']:.2f}\n"
            f"多方防守價：{p['t_sup']:.2f}\n"
            f"━━━━━━━━━━━━━\n"
            f"前日關鍵價\n"
            f"空方防守價：{p['p_res']:.2f}\n"
            f"關鍵價：{p['p_key']:.2f}\n"
            f"多方防守價：{p['p_sup']:.2f}\n"
            f"━━━━━━━━━━━━━\n"
            f"周關鍵價：{p['w_key']:.2f}\n"
            f"月關鍵價：{p['m_key']:.2f}"
        )
        send_line_reply(reply_token, report_text)
    except Exception as e:
        print(f"LINE 回覆出錯: {e}")
        send_line_reply(reply_token, "❌ 系統計算發生錯誤，請稍後再試。")

def send_line_reply(reply_token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    httpx.post(url, json=payload, headers=headers)

if __name__ == "__main__":
    server = http.server.HTTPServer(('0.0.0.0', 10000), LineWebhookHandler)
    print("🟢 LINE 機器人 Webhook 伺服器已在連接埠 10000 啟動...")
    server.serve_forever()
