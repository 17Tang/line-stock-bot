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

# ==========================================
# ⚙️ 核心設定區
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# ==========================================
# 📊 數據下載與關鍵價計算邏輯
# ==========================================
def calculate_stock_prices(stock_id):
    days_back = 365
    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=1)
    start_date = today - datetime.timedelta(days=days_back)

    if len(stock_id) >= 4 and stock_id.isdigit():
        ticker_id = f"{stock_id}.TW"
        df_daily = yf.download(ticker_id, start=start_date, end=end_date, progress=False)
        if df_daily.empty:
            ticker_id = f"{stock_id}.TWO"
            df_daily = yf.download(ticker_id, start=start_date, end=end_date, progress=False)
    else:
        ticker_id = stock_id.upper()
        df_daily = yf.download(ticker_id, start=start_date, end=end_date, progress=False)

    if df_daily.empty or len(df_daily) < 2:
        return None

    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)

    t_day = df_daily.iloc[-1]
    p_day = df_daily.iloc[-2]
    
    t_h, t_l, t_c = float(t_day["High"]), float(t_day["Low"]), float(t_day["Close"])
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

    return {
        "ticker_id": ticker_id, "current": t_c,
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
# ✉️ LINE 訊息回覆傳送邏輯（防洗版暗號版）
# ==========================================
def process_and_reply_line(reply_token, user_text):
    if user_text == "開始" or user_text.lower() == "hello":
        send_line_reply(reply_token, "👋 歡迎使用關鍵價看盤助手！\n\n💡 為了防洗版，現在請在股號前加一個『#』。\n👉 例如輸入：`#2330` 或 `#AAPL` 即可查價。")
        return

    # ⚡ 檢查暗號
    if not user_text.startswith("#"):
        return

    stock_id = user_text[1:].strip()
    if not stock_id:
        return

    try:
        p = calculate_stock_prices(stock_id)
        if p is None:
            send_line_reply(reply_token, f"❌ 找不到股票代號 '{stock_id}' 的資料，請確認是否輸入正確。")
            return

        report_text = (
            f"📊 股票標的：{p['ticker_id']}\n"
            f"🟧 股票現價：{p['current']:.2f}\n"
            f"━━━━━━━━━━━━━\n"
            f"📅 【今日技術指標】\n"
            f"🟥 今日壓力：{p['t_res']:.2f}\n"
            f"🟨 今日關鍵：{p['t_key']:.2f}\n"
            f"🟩 今日支撐：{p['t_sup']:.2f}\n"
            f"━━━━━━━━━━━━━\n"
            f"⏳ 【前日技術指標】\n"
            f"🛑 前日壓力：{p['p_res']:.2f}\n"
            f"🪙 前日關鍵：{p['p_key']:.2f}\n"
            f"❇️ 前日支撐：{p['p_sup']:.2f}\n"
            f"━━━━━━━━━━━━━\n"
            f"📈 【長線波段參考】\n"
            f"🔷 周關鍵價：{p['w_key']:.2f}\n"
            f"🔶 月關鍵價：{p['m_key']:.2f}"
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
