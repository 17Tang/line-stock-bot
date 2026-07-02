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
import numpy as np

# ==========================================
# ⚙️ 核心設定區
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# 🔥 100% 恢復原本最強的防封鎖偽裝 Session，解鎖個股查詢
yf_session = requests.Session()
yf_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})

# ==========================================
# 📊 數據下載與關鍵價計算 (初心回歸 + 輕量即時補丁版)
# ==========================================
def calculate_stock_prices(stock_id):
    days_back = 365
    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=1)
    start_date = today - datetime.timedelta(days=days_back)
    
    target = stock_id.upper().strip()
    
    # 精準判定資產類型與官方 MiS 對應代號
    if target in ["TWII", "^TWII"]:
        yf_id = "^TWII"
        is_index = True
        is_tw_stock = False
        mis_ch = "tse_t00.tw"
    elif target in ["TWOII", "^TWOII"]:
        yf_id = "^TWOII"
        is_index = True
        is_tw_stock = False
        mis_ch = "otc_o00.tw"
    else:
        is_index = False
        is_tw_stock = target.replace(".", "").isdigit() and len(target) >= 4
        yf_id = f"{target}.TW" if is_tw_stock else target
        mis_ch = f"tse_{target}.tw"

    print(f"--- 查詢代號確認: {yf_id} ---")

    # 🚀 步驟一：用原本最穩定的 yf.download 下載歷史基礎
    try:
        df_daily = yf.download(yf_id, start=start_date, end=end_date, progress=False, session=yf_session)
        if is_tw_stock and df_daily.empty:
            yf_id = f"{target}.TWO"
            df_daily = yf.download(yf_id, start=start_date, end=end_date, progress=False, session=yf_session)
            mis_ch = f"otc_{target}.tw"
    except Exception as e:
        print(f"Yahoo 歷史下載失敗: {e}")
        return None

    if df_daily.empty or len(df_daily) < 2:
        return None

    if isinstance(df_daily.columns, pd.MultiIndex):
        df_daily.columns = df_daily.columns.get_level_values(0)

    # 🚀 步驟二：解決 Yahoo 大盤卡死凍結地雷 (若歷史最新日期落後今天，用官方 MiS 即時補齊)
    try:
        latest_date = df_daily.index[-1].date()
        if latest_date < today and (is_index or is_tw_stock):
            print(f"⚠️ 偵測到 Yahoo 日期延遲 ({latest_date})，強行調用台灣官方 MiS 補丁...")
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={mis_ch}"
            res = requests.get(url, headers=yf_session.headers, timeout=5).json()
            
            if "msgArray" in res and len(res["msgArray"]) > 0:
                info = res["msgArray"][0]
                price_str = info.get("z") or info.get("v") or info.get("o")
                
                if price_str and price_str != "-":
                    live_dt = pd.to_datetime(info["d"])
                    live_high = float(info["h"].replace(',', ''))
                    live_low = float(info["l"].replace(',', ''))
                    live_close = float(price_str.replace(',', ''))
                    
                    # 強行寫入今日最新 K 線
                    df_daily.loc[live_dt, "High"] = live_high
                    df_daily.loc[live_dt, "Low"] = live_low
                    df_daily.loc[live_dt, "Close"] = live_close
                    
                    # 清除重複並重新排序
                    df_daily = df_daily[~df_daily.index.duplicated(keep='last')]
                    df_daily.sort_index(inplace=True)
                    print(f"🟢 成功補齊今日 ({info['d']}) 官方即時數據！")
    except Exception as e:
        print(f"⚠️ 嘗試即時補丁無回應 (無礙後續計算): {e}")

    # 僅檢查 NaN 空棒
    if pd.isna(df_daily.iloc[-1]["Close"]) or np.isnan(float(df_daily.iloc[-1]["Close"])):
        df_daily = df_daily.iloc[:-1]

    t_day = df_daily.iloc[-1]
    p_day = df_daily.iloc[-2]
    
    t_h, t_l, t_c = float(t_day["High"]), float(t_day["Low"]), float(t_day["Close"])
    p_h, p_l, p_c = float(p_day["High"]), float(p_day["Low"]), float(p_day["Close"])

    current_price = t_c
    yesterday_close = p_c
    quote_time = df_daily.index[-1].strftime("%Y-%m-%d")

    # 漲跌計算
    change_points = current_price - yesterday_close
    change_percent = (change_points / yesterday_close) * 100
    
    if change_points > 0:
        change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
    elif change_points < 0:
        change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
    else:
        change_str = f"─ 0.00 (0.00%)"

    # 關鍵價公式
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

    # 名稱轉換
    stock_name = ""
    if yf_id == "^TWII":
        stock_name = "上市加權指數"
    elif yf_id == "^TWOII":
        stock_name = "櫃買指數"
    elif is_tw_stock:
        try:
            tw_info = twstock.codes.get(target)
            if tw_info: stock_name = tw_info.name
        except Exception: pass
        
    if not stock_name:
        try:
            stock_name = yf.Ticker(yf_id, session=yf_session).info.get("shortName", target)
        except Exception:
            stock_name = target

    display_name = f"{yf_id} {stock_name}"

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

        current = p['current']
        
        # ⚡ 依昨日關鍵價進行多空階層判斷 (精準文字指定版)
        if current < p['p_sup']:
            status_yesterday = "🚨 跌破多防價 極度空頭"
        elif current < p['p_key']:
            status_yesterday = "🟡 小於關鍵價 未破多防"
        elif current <= p['p_res']:
            status_yesterday = "🔵 大於關鍵價 未達空防"
        else:
            status_yesterday = "🔥 漲過空防價 強勢多頭"

        # 判斷是否大於周、月關鍵價
        status_week = "🔴 低於周關鍵價" if current < p['w_key'] else "🟢 站上周關鍵價"
        status_month = "🔴 低於月關鍵價" if current < p['m_key'] else "🟢 站上月關鍵價"

        report_text = (
            f"{p['ticker_id']}\n"
            f"{p['current']:.2f} {p['change_str']}\n"
            f"{p['quote_time']}\n"
            f"━━━━━━━━━━━━━\n"
            f"【即時多空狀態】\n"
            f"{status_yesterday}\n"
            f"{status_week}\n"
            f"{status_month}\n"
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
