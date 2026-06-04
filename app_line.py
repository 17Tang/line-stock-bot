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
# 📊 數據下載與關鍵價計算邏輯 (1分鐘K線絕對精準版)
# ==========================================
def calculate_stock_prices(stock_id):
    tw_tz = pytz.timezone("Asia/Taipei")
    
    clean_target = stock_id.upper().replace("^", "").strip()
    is_tw_index = clean_target in ["TWII", "TWOII"]
    is_tw_stock = len(clean_target) >= 4 and clean_target.isdigit()

    if is_tw_index:
        yf_id = f"^{clean_target}"
    elif is_tw_stock:
        yf_id = f"{clean_target}.TW"
    else:
        yf_id = clean_target

    print(f"\n--- 查詢開始: {yf_id} ---")

    try:
        # 1. 同時下載「歷史日K」與「即時1分鐘K」
        df_daily = yf.download(yf_id, period="3mo", progress=False, session=yf_session)
        df_min = yf.download(yf_id, period="5d", interval="1m", progress=False, session=yf_session)
        
        # 台灣上市找不到，自動轉上櫃
        if is_tw_stock and (df_daily.empty or df_min.empty):
            yf_id = f"{clean_target}.TWO"
            df_daily = yf.download(yf_id, period="3mo", progress=False, session=yf_session)
            df_min = yf.download(yf_id, period="5d", interval="1m", progress=False, session=yf_session)
    except Exception as e:
        print(f"下載失敗: {e}")
        return None

    if df_daily.empty or df_min.empty or len(df_daily) < 3:
        return None

    # 清理 Yahoo 回傳的多層索引 (MultiIndex)
    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)
    if isinstance(df_min.columns, pd.MultiIndex):
        df_min.columns = df_min.columns.get_level_values(0)

    # ⚡ 2. 提取「絕對真實」的報價時間與現價 (不再使用 current time 假冒)
    # 將 1分鐘 K 線的最後一根時間，轉換為台灣時區
    last_min_time = df_min.index[-1].astimezone(tw_tz)
    quote_time = last_min_time.strftime("%Y-%m-%d %H:%M:%S")
    trade_date = last_min_time.date() # 這筆價格發生的真實日期
    
    # 提取精準現價
    current_price = float(df_min.iloc[-1]["Close"])

    # ⚡ 3. 精準切割「昨日」與「今日」的數據
    # 確保 df_daily 的時區被清理，方便做日期比對
    df_daily.index = pd.to_datetime(df_daily.index).tz_localize(None)
    
    # 嚴格定義「昨日」：日期必須小於剛剛抓到的最新 trade_date
    past_daily = df_daily[df_daily.index.date < trade_date]
    if past_daily.empty:
        return None
        
    p_day = past_daily.iloc[-1]
    yesterday_close = float(p_day["Close"])
    p_h, p_l = float(p_day["High"]), float(p_day["Low"])

    # 嚴格定義「今日」：從 1分鐘 K 線中，篩選出屬於 trade_date 當天的所有 K 棒
    today_min_bars = df_min[df_min.index.astimezone(tw_tz).date == trade_date]
    
    # 從今日所有的分鐘 K 棒中，動態找出今天的最高與最低點 (完美解決盤中即時跳動)
    t_h = float(today_min_bars["High"].max())
    t_l = float(today_min_bars["Low"].min())
    
    # 防呆機制：確保高低點必定包覆現價
    t_h = max(t_h, current_price)
    t_l = min(t_l, current_price)

    # 4. 漲跌點數與百分比計算
    change_points = current_price - yesterday_close
    change_percent = (change_points / yesterday_close) * 100
    
    if change_points > 0:
        change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
    elif change_points < 0:
        change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
    else:
        change_str = f"─ 0.00 (0.00%)"

    # 5. 關鍵價核心公式
    # 今日關鍵價 (盤中會隨 t_h, t_l 即時跳動)
    t_res = t_h + (t_h - t_l) * 0.382
    t_key = (t_h + t_l) / 2
    t_sup = t_l - (t_h - t_l) * 0.382

    # 前日關鍵價 (絕對死鎖在昨日數據)
    p_res = p_h + (p_h - p_l) * 0.382
    p_key = (p_h + p_l) / 2
    p_sup = p_l - (p_h - p_l) * 0.382

    # 6. 周月線計算 (將今日即時K棒組合進歷史，確保周月線不斷層)
    new_row = pd.DataFrame({
        "High": [t_h], "Low": [t_l]
    }, index=[pd.Timestamp(trade_date)])
    full_daily = pd.concat([past_daily, new_row])

    df_weekly = full_daily.resample("W-FRI").agg({"High": "max", "Low": "min"}).dropna()
    w_key = float((df_weekly.iloc[-1]["High"] + df_weekly.iloc[-1]["Low"]) / 2)

    df_monthly = full_daily.resample("ME").agg({"High": "max", "Low": "min"}).dropna()
    m_key = float((df_monthly.iloc[-1]["High"] + df_monthly.iloc[-1]["Low"]) / 2)

    # 7. 名稱轉換
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

        report_text = (
            f"{p['ticker_id']}\n"
            f"{p['current']:.2f} {p['change_str']}\n"
            f"{p['quote_time']}\n"
            f"━━━━━━━━━━━━━\n"
            f"
