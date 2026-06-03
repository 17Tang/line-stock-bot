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
import twstock
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
    
    # 1. 判斷是否為台股與大盤
    is_tw_stock = len(stock_id) >= 4 and stock_id.isdigit()
    is_tw_index = stock_id.upper() in ["^TWII", "^TWOII"]

    if is_tw_stock:
        ticker_id = f"{stock_id}.TW"
    else:
        ticker_id = stock_id.upper()

    # ⚡⚡⚡ 【終極大改版：直接下載歷史日K + 分鐘K，雙重交叉提取】 ⚡⚡⚡
    try:
        # 下載歷史日K線 (用來計算長線周月關鍵價、以及前一天的指標)
        df_daily = yf.download(ticker_id, period="1mo", progress=False)
        # 下載超精準 1分鐘K線 (用來抓取今日最新的真實報價、今日最高/最低、以及精準到分秒的時間)
        # 抓最近 5 天以防遇到週一或連續假期
        ticker_obj = yf.Ticker(ticker_id)
        df_min = ticker_obj.history(period="5d", interval="1m")
    except Exception:
        return None

    if df_daily.empty or df_min.empty or len(df_daily) < 3:
        # 如果是台股特定上市櫃板塊，改試 .TWO
        if is_tw_stock and ticker_id.endswith(".TW"):
            ticker_id = f"{stock_id}.TWO"
            try:
                df_daily = yf.download(ticker_id, period="1mo", progress=False)
                df_min = yf.Ticker(ticker_id).history(period="5d", interval="1m")
            except Exception:
                return None
        else:
            return None

    # 清理多層索引欄位
    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)
    if isinstance(df_min.columns, pd.MultiIndex):
        df_min.columns = df_min.columns.get_level_values(0)

    # 🔍 【一、提取今日即時報價與精準時間】
    # 1. 直接抓 1分鐘K線的最後一筆，這 100% 就是目前 Yahoo 擁有的最新成交價
    latest_bar = df_min.iloc[-1]
    current_price = float(latest_bar["Close"])
    
    # 2. 如實呈現這筆價格的真實撮合時間 (轉為台灣時間格式)
    # 不論盤中 9:50、收盤 13:30，甚至 Yahoo 大盤卡住，這裡抓到幾點，時間就顯示幾點
    raw_time = df_min.index[-1]
    quote_time_tw = raw_time.astimezone(tw_tz)
    quote_time = quote_time_tw.strftime("%Y-%m-%d %H:%M:%S")

    # 🔍 【二、動態抓取今日交易日與昨日交易日的 K 線數據】
    # 取得今天這筆價格所屬的「年月日日期」字串
    current_trade_date_str = quote_time_tw.strftime("%Y-%m-%d")
    
    # 將日K線重新整理
    import numpy as np
    if pd.isna(df_daily.iloc[-1]["Close"]) or df_daily.iloc[-1]["Volume"] == 0 or np.isnan(df_daily.iloc[-1]["Close"]):
        df_daily = df_daily.iloc[:-1]

    # 比對日K線最後一筆的日期是否已經是今天
    last_daily_date_str = df_daily.index[-1].strftime("%Y-%m-%d")
    
    if last_daily_date_str == current_trade_date_str:
        # 狀況 A：日K已經包含今天 (個股通常是這樣)
        t_day = df_daily.iloc[-1]
        p_day = df_daily.iloc[-2]
    else:
        # 狀況 B：日K還卡在昨天 (大盤指數晚上最常發生！)
        # 此時歷史日K的最後一筆其實是「前一日」，所以我們把一分鐘K整合出今天的最高最低
        p_day = df_daily.iloc[-1]
        # 從 1分鐘K線中，篩選出屬於今天這個交易日的所有資料，結合成今日日K
        df_today_min = df_min[df_min.index.date == raw_time.date()]
        
        # 建立一個虛擬的今日日K物件
        t_day = {
            "High": float(df_today_min["High"].max()) if not df_today_min.empty else float(p_day["High"]),
            "Low": float(df_today_min["Low"].min()) if not df_today_min.empty else float(p_day["Low"]),
            "Close": current_price
        }

    # 🔍 【三、計算漲跌幅】
    yesterday_close = float(p_day["Close"])
    change_points = current_price - yesterday_close
    change_percent = (change_points / yesterday_close) * 100
    
    if change_points > 0:
        change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
    elif change_points < 0:
        change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
    else:
        change_str = f"─ 0.00 (0.00%)"

    # 🔍 【四、計算公式】
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

    # 獲取中文名稱
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
            f"【今日關鍵價】\n"
            f"空方防守價：{p['t_res']:.2f}\n"
            f"關鍵價：{p['t_key']:.2f}\n"
            f"多方防守價：{p['t_sup']:.2f}\n"
            f"━━━━━━━━━━━━━\n"
            f"【前日關鍵價】\n"
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
