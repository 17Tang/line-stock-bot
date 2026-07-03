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
import twstock
import requests
import numpy as np

# ==========================================
# ⚙️ 核心設定區
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# ==========================================
# 📊 數據下載與關鍵價計算 (FinMind 終極穩定版)
# ==========================================
def calculate_stock_prices(stock_id):
    try:
        days_back = 365
        today = datetime.date.today()
        start_date = today - datetime.timedelta(days=days_back)
        
        target = stock_id.upper().strip()
        
        # ⚡ 1. 精準對齊 FinMind 的大盤代號與官方 MiS 代號
        if target in ["TWII", "^TWII", "TAIEX"]:
            finmind_id = "TAIEX"
            mis_ch = "tse_t00.tw"
            display_name = "^TWII 上市加權指數"
            is_tw_stock = False
        elif target in ["TWOII", "^TWOII", "TWO"]:
            finmind_id = "TWO"
            mis_ch = "otc_o00.tw"
            display_name = "^TWOII 櫃買指數"
            is_tw_stock = False
        else:
            finmind_id = target
            is_tw_stock = target.replace(".", "").isdigit() and len(target) >= 4
            display_name = target
            mis_ch = f"tse_{target}.tw"  # 預設上市

        # ⚡ 2. 如果是個股，利用 twstock 自動判斷上市/上櫃，精準導航 MiS 即時補丁路徑
        if is_tw_stock:
            try:
                tw_info = twstock.codes.get(target)
                if tw_info:
                    display_name = f"{target} {tw_info.name}"
                    if tw_info.market == "上櫃":
                        mis_ch = f"otc_{target}.tw"
                    else:
                        mis_ch = f"tse_{target}.tw"
            except Exception:
                pass

        print(f"--- FinMind 查詢確認: {finmind_id} (MiS補丁通道: {mis_ch}) ---")

        # 🚀 路線一：直連 FinMind 獲取歷史大數據
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": finmind_id,
            "start_date": start_date.strftime("%Y-%m-%d")
        }
        
        res = requests.get(url, params=params, timeout=10).json()
        if res.get("msg") != "success" or not res.get("data"):
            print(f"❌ FinMind 未能取得 {finmind_id} 的歷史資料")
            return None
            
        # 轉換為標準 DataFrame
        df_raw = pd.DataFrame(res["data"])
        df_raw['date'] = pd.to_datetime(df_raw['date'])
        df_raw.set_index('date', inplace=True)
        
        # ⚡ 關鍵清洗：將 FinMind 的欄位名 (max, min, close) 對齊原本的運算邏輯 (High, Low, Close)
        df_daily = pd.DataFrame(index=df_raw.index)
        df_daily['High'] = df_raw['max'].astype(float)
        df_daily['Low'] = df_raw['min'].astype(float)
        df_daily['Close'] = df_raw['close'].astype(float)

        if df_daily.empty or len(df_daily) < 2:
            return None

        mis_yesterday_close = None

        # 🚀 路線二（盤中即時補丁）：如果 FinMind 最新歷史落後今天，調用官方 MiS 秒速補齊
        try:
            latest_date = df_daily.index[-1].date()
            if latest_date < today:
                print(f"⚠️ 歷史數據日期 ({latest_date}) 尚未更新，強行載入 MiS 盤中即時補丁...")
                mis_url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={mis_ch}"
                mis_res = requests.get(mis_url, timeout=5).json()
                
                if "msgArray" in mis_res and len(mis_res["msgArray"]) > 0:
                    info = mis_res["msgArray"][0]
                    price_str = info.get("z") or info.get("v") or info.get("o")
                    
                    if price_str and price_str != "-":
                        live_dt = pd.to_datetime(info["d"])
                        live_high = float(info["h"].replace(',', ''))
                        live_low = float(info["l"].replace(',', ''))
                        live_close = float(price_str.replace(',', ''))
                        
                        # 從官方擷取昨收參考價，防止斷流日差導致漲跌算錯
                        if info.get("y") and info["y"] != "-":
                            mis_yesterday_close = float(info["y"].replace(',', ''))
                        
                        # 壓入 DataFrame 尾端
                        df_daily.loc[live_dt] = [live_high, live_low, live_close]
                        df_daily = df_daily[~df_daily.index.duplicated(keep='last')]
                        df_daily.sort_index(inplace=True)
                        print(f"🟢 成功補齊今日 MiS 即時數據！昨收參考價為: {mis_yesterday_close}")
        except Exception as e:
            print(f"⚠️ 嘗試即時補丁無回應 (無礙後續歷史計算): {e}")

        # 僅檢查 NaN 空棒
        if pd.isna(df_daily.iloc[-1]["Close"]) or np.isnan(float(df_daily.iloc[-1]["Close"])):
            df_daily = df_daily.iloc[:-1]

        t_day = df_daily.iloc[-1]
        p_day = df_daily.iloc[-2]
        
        t_h, t_l, t_c = float(t_day["High"]), float(t_day["Low"]), float(t_day["Close"])
        p_h, p_l, p_c = float(p_day["High"]), float(p_day["Low"]), float(p_day["Close"])

        current_price = t_c
        
        # 精準漲跌校正
        yesterday_close = mis_yesterday_close if mis_yesterday_close is not None else p_c
        quote_time = df_daily.index[-1].strftime("%Y-%m-%d")

        change_points = current_price - yesterday_close
        change_percent = (change_points / yesterday_close) * 100
        
        if change_points > 0:
            change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
        elif change_points < 0:
            change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
        else:
            change_str = f"─ 0.00 (0.00%)"

        # 原汁原味關鍵價核心公式
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

        return {
            "ticker_id": display_name,
            "current": current_price,
            "change_str": change_str,
            "quote_time": quote_time,
            "t_res": t_res, "t_key": t_key, "t_sup": t_sup,
            "p_res": p_res, "p_key": p_key, "p_sup": p_sup,
            "w_key": w_key, "m_key": m_key
        }
    except Exception as e:
        print(f"核心計算異常: {e}")
        return None

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
        
        # 昨日關鍵價指定多空階層判斷文字
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
