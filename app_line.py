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
# 📊 數據下載與關鍵價計算邏輯 (指數與個股完全獨立)
# ==========================================
def calculate_stock_prices(stock_id):
    tw_tz = pytz.timezone("Asia/Taipei")
    now_tw = datetime.datetime.now(tw_tz)
    
    # ⚡ 核心修復：在最源頭直接將輸入字串轉為去空白大寫，確保分流判斷萬無一失
    target = stock_id.upper().strip()
    is_tw_index = target in ["^TWII", "^TWOII"]
    is_tw_stock = len(target) >= 4 and target.isdigit()

    print(f"\n================ [ 測試日誌啟動 ] ================")
    print(f"原始輸入: {stock_id} -> 處理後目標: {target}")
    print(f"大盤指數判定: {is_tw_index} | 台灣個股判定: {is_tw_stock}")

    # ⚡⚡⚡ 【第一部分：大盤指數獨立撰寫邏輯】 ⚡⚡⚡
    if is_tw_index:
        print(f"進入【大盤指數】獨立計算通道...")
        try:
            # 優先嘗試向 yfinance 下載歷史日K (指數專用)
            df_index_hist = yf.download(target, period="1mo", progress=False)
            if isinstance(df_index_hist.columns, pd.MultiIndex):
                df_index_hist.columns = df_index_hist.columns.get_level_values(0)
                
            # 換日防禦：如果最後一根歷史K是今天，代表是舊的未清算空棒，予以剔除
            if not df_index_hist.empty and df_index_hist.index[-1].strftime("%Y-%m-%d") == now_tw.strftime("%Y-%m-%d"):
                df_index_hist = df_index_hist.iloc[:-1]
                
            p_day = df_index_hist.iloc[-1]
            yesterday_close = float(p_day["Close"])
            p_h = float(p_day["High"])
            p_l = float(p_day["Low"])
            
            # 嘗試抓取官方即時數據
            rt_id = "tse_^TWII" if target == "^TWII" else "otc_^TWOII"
            rt_data = twstock.realtime.get(rt_id)
            
            # ⚡ 盤前防禦：如果開盤前 twstock 拒絕回傳，自動採用備用日K數據
            if rt_data and rt_data.get('success'):
                print(f"twstock 指數即時數據獲取成功！")
                info = rt_data['info']
                realtime_info = rt_data['realtime']
                
                time_str = info.get('time', '')
                quote_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S") if time_str else f"{df_index_hist.index[-1].strftime('%Y-%m-%d')} 13:30:00"
                
                current_price = realtime_info.get('latest_trade_price')
                t_h = realtime_info.get('high')
                t_l = realtime_info.get('low')
                open_price = realtime_info.get('open')
                
                import numpy as np
                if current_price is None or current_price == '-' or np.isnan(float(current_price)): current_price = open_price if open_price and open_price != '-' else yesterday_close
                if t_h is None or t_h == '-' or np.isnan(float(t_h)): t_h = current_price
                if t_l is None or t_l == '-' or np.isnan(float(t_l)): t_l = current_price
                
                current_price = float(current_price)
                t_h = float(t_h)
                t_l = float(t_l)
            else:
                print(f"⚠️ twstock 盤前或超時拒絕，自動啟動大盤日K緩衝備用機制...")
                current_price = yesterday_close
                t_h = float(df_index_hist.iloc[-1]["High"])
                t_l = float(df_index_hist.iloc[-1]["Low"])
                quote_time = f"{df_index_hist.index[-1].strftime('%Y-%m-%d')} 13:30:00"
            
            display_name = "上市加權指數" if target == "^TWII" else "櫃買指數"
            display_name = f"{target} {display_name}"
            
        except Exception as e:
            print(f"❌ 指數核心通道崩潰: {e}")
            return None

    # ⚡⚡⚡ 【第二部分：一般個股/美股獨立撰寫邏輯】 ⚡⚡⚡
    else:
        print(f"進入【個股/美股】獨立計算通道...")
        ticker_id = f"{target}.TW" if is_tw_stock else target
        try:
            df_daily = yf.download(ticker_id, period="1mo", progress=False)
            if is_tw_stock and df_daily.empty:
                ticker_id = f"{target}.TWO"
                df_daily = yf.download(ticker_id, period="1mo", progress=False)
        except Exception as e:
            print(f"❌ 歷史日K線下載失敗: {e}")
            return None

        if df_daily.empty or len(df_daily) < 3:
            print(f"❌ 找不到該標的的日K線歷史數據！")
            return None

        if isinstance(df_daily.columns, pd.MultiIndex):
            df_daily.columns = df_daily.columns.get_level_values(0)

        import numpy as np
        if pd.isna(df_daily.iloc[-1]["Close"]) or df_daily.iloc[-1]["Volume"] == 0 or np.isnan(df_daily.iloc[-1]["Close"]):
            df_daily = df_daily.iloc[:-1]

        if is_tw_stock:
            try:
                rt_id = f"otc_{target}.tw" if ticker_id.endswith(".TWO") else f"tse_{target}.tw"
                rt_data = twstock.realtime.get(rt_id)
                if rt_data and rt_data.get('success'):
                    realtime_info = rt_data['realtime']
                    current_price = realtime_info.get('latest_trade_price')
                    if current_price is None or current_price == '-' or np.isnan(float(current_price)):
                        current_price = df_daily.iloc[-1]["Close"]
                    current_price = float(current_price)
                    
                    time_str = rt_data['info'].get('time', '')
                    quote_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S") if time_str else f"{df_daily.index[-1].strftime('%Y-%m-%d')} 13:30:00"
                else:
                    raise Exception()
            except Exception:
                current_price = float(df_daily.iloc[-1]["Close"])
                quote_time = f"{df_daily.index[-1].strftime('%Y-%m-%d')} 13:30:00"
                
            yesterday_close = float(df_daily.iloc[-2]["Close"])
            t_day = df_daily.iloc[-1]
            p_day = df_daily.iloc[-2]
            t_h, t_l = float(t_day["High"]), float(t_day["Low"])
            p_h, p_l = float(p_day["High"]), float(p_day["Low"])
        else:
            # 美股邏輯
            t_day = df_daily.iloc[-1]
            p_day = df_daily.iloc[-2]
            current_price = float(t_day["Close"])
            yesterday_close = float(p_day["Close"])
            t_h, t_l = float(t_day["High"]), float(t_day["Low"])
            p_h, p_l = float(p_day["High"]), float(p_day["Low"])
            quote_time = df_daily.index[-1].strftime("%Y-%m-%d 16:00:00")

        stock_name = ""
        if is_tw_stock:
            try:
                tw_info = twstock.codes.get(target)
                if tw_info: stock_name = tw_info.name
            except Exception: pass
        if not stock_name:
            try:
                stock_name = yf.Ticker(ticker_id).info.get("shortName", target)
            except Exception: stock_name = target
            
        display_name = f"{target} {stock_name}"

    # ⚡⚡⚡ 【第三部分：數據終端列印檢查與核心公式】 ⚡⚡⚡
    print(f"--- 數據檢查斷點 (Data Print Check) ---")
    print(f"顯示名稱: {display_name}")
    print(f"計算現價: {current_price} | 昨收價: {yesterday_close}")
    print(f"今日高點: {t_h} | 今日低點: {t_l}")
    print(f"前日高點: {p_h} | 前日低點: {p_l}")
    print(f"報價時間: {quote_time}")
    print(f"================================================\n")

    # 漲跌點數與百分比計算
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

    hist_df = df_index_hist if is_tw_index else df_daily
    df_weekly = hist_df.resample("W-FRI").agg({"High": "max", "Low": "min"})
    w_key = float((df_weekly.iloc[-1]["High"] + df_weekly.iloc[-1]["Low"]) / 2)

    df_monthly = hist_df.resample("ME").agg({"High": "max", "Low": "min"})
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
# ✉️ LINE 訊息回覆傳送邏輯 (移除多餘標題)
# ==========================================
def process_and_reply_line(reply_token, user_text):
    if user_text == "開始" or user_text.lower() == "hello":
        send_line_reply(reply_token, "歡迎使用關鍵價看盤助手！\n\n💡 請在股號前加一個『#』即可查詢。\n👉 例如輸入：#2330 或 #^TWII")
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

        # ⚡ 遵照吩咐：完全拿掉 【標的】：、 【現價】：、 【時間】： 這三個中文字標題
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
        "Content-Type": "https://api.line.me/v2/bot/message/reply",
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
