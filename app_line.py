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
import requests

# ==========================================
# ⚙️ 核心設定區
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# ⚡ 防封鎖偽裝：避免 Yahoo API 拒絕連線
yf_session = requests.Session()
yf_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})

# ==========================================
# 📊 數據下載與關鍵價計算 (與 Streamlit 完全一致版)
# ==========================================
def calculate_stock_prices(stock_id):
    days_back = 365
    today = datetime.date.today()
    # ⚡ 完美對齊 Streamlit：結束日期設為明天，確保抓到最新日 K
    end_date = today + datetime.timedelta(days=1)
    start_date = today - datetime.timedelta(days=days_back)
    
    clean_target = stock_id.upper().replace("^", "").strip()
    is_tw_index = clean_target in ["TWII", "TWOII"]
    is_tw_stock = len(clean_target) >= 4 and clean_target.isdigit()

    if is_tw_index:
        yf_id = f"^{clean_target}"
    elif is_tw_stock:
        yf_id = f"{clean_target}.TW"
    else:
        yf_id = clean_target

    # 1. 統一使用 yfinance 下載日 K
    try:
        df_daily = yf.download(yf_id, start=start_date, end=end_date, progress=False, session=yf_session)
        if is_tw_stock and df_daily.empty:
            yf_id = f"{clean_target}.TWO"
            df_daily = yf.download(yf_id, start=start_date, end=end_date, progress=False, session=yf_session)
    except Exception:
        return None

    if df_daily.empty or len(df_daily) < 2:
        return None

    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)

    # 換日空值防禦 (確保拿到有效的 K 棒)
    import numpy as np
    if pd.isna(df_daily.iloc[-1]["Close"]) or df_daily.iloc[-1]["Volume"] == 0 or np.isnan(df_daily.iloc[-1]["Close"]):
        df_daily = df_daily.iloc[:-1]

    # ⚡ 2. 數據提取 (完全對齊 Streamlit 邏輯)
    t_day = df_daily.iloc[-1]
    p_day = df_daily.iloc[-2]
    
    t_h, t_l, t_c = float(t_day["High"]), float(t_day["Low"]), float(t_day["Close"])
    p_h, p_l, p_c = float(p_day["High"]), float(p_day["Low"]), float(p_day["Close"])

    current_price = t_c
    yesterday_close = p_c
    
    # 時間不再硬擠分秒，直接顯示乾淨的 K 棒日期
    quote_time = df_daily.index[-1].strftime("%Y-%m-%d")

    # 3. 漲跌點數與百分比計算
    change_points = current_price - yesterday_close
    change_percent = (change_points / yesterday_close) * 100
    
    if change_points > 0:
        change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
    elif change_points < 0:
        change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
    else:
        change_str = f"─ 0.00 (0.00%)"

    # 4. 關鍵價核心公式
    t_res = t_h + (t_h - t_l) * 0.382
    t_key = (t_h + t_l) / 2
    t_sup = t_l - (t_h - t_l) * 0.382

    p_res = p_h + (p_h - p_l) * 0.382
    p_key = (p_h + p_l) / 2
    p_sup = p_l - (p_h - p_l) * 0.382

    # 周月線計算
    df_weekly = df_daily.resample("W-FRI").agg({"High": "max", "Low": "min"}).dropna()
    w_key = float((df_weekly.iloc[-1]["High"] + df_weekly.iloc[-1]["Low"]) / 2)

    df_monthly = df_daily.resample("ME").agg({"High": "max", "Low": "min"}).dropna()
    m_key = float((df_monthly.iloc[-1]["High"] + df_monthly.iloc[-1]["Low"]) / 2)

    # 5. 名稱轉換
    stock_name = ""
    if is_tw_index:
        stock_name = "上市加權指數" if clean_target == "TWII" else "櫃買指數"
    elif is_tw_stock:
        try:
            tw_info = twstock.codes.get(clean_target)
            if tw_info: stock_name = tw_info.name
        except Exception: pass
        
    if not stock_name:
        try:
            stock_name = yf.Ticker(yf_id, session=yf_session).info.get("shortName", clean_target)
        except Exception:
            stock_name = clean_target

    display_target = f"^{clean_target}" if is_tw_index else clean_target
    display_name = f"{display_target} {stock_name}"

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
        send_line_reply(reply_token, "歡迎使用關鍵價看盤助手！\n\n請在股號前加一個『#』即可查詢。\n例如輸入：#2330 或 #TWII")
        return

    if not user_text.startswith("#"):
        return

    stock_id = user_text[1:].strip()
    if not stock_id:
        return

    try:
        p = calculate_stock_prices(stock_id)
        if p is None:
            send_line_reply(reply_token, f"❌ 找不到 '{stock_id}' 的資料，或伺服器目前遭限流，請稍後再試。")
            return

        # ⚡ 純淨無標題格式，時間僅顯示日期
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
