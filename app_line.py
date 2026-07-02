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

# 官方 API 專用 Session (僅用於直連台灣官方源)
gov_session = requests.Session()
gov_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})

# ==========================================
# 🇹🇼 方案 B：台灣官方 API 歷史資料抓取器 (網址精準校正版)
# ==========================================
def get_taiwan_official_index(is_tpex=False):
    """
    直接從台灣證交所/櫃買中心新版網站抓取歷史大盤 JSON 資料。
    """
    today = datetime.date.today()
    first_day_this_month = today.replace(day=1)
    last_month_end = first_day_this_month - datetime.timedelta(days=1)
    first_day_last_month = last_month_end.replace(day=1)
    two_months_ago_end = first_day_last_month - datetime.timedelta(days=1)
    first_day_two_months_ago = two_months_ago_end.replace(day=1)

    dates_to_fetch = [first_day_two_months_ago, first_day_last_month, first_day_this_month]
    df_list = []

    for d in dates_to_fetch:
        try:
            if not is_tpex:
                # 證交所 (上市加權)
                date_str = d.strftime("%Y%m%d")
                url = f"https://www.twse.com.tw/zh/indicesReport/MI_5MINS_HIST?response=json&date={date_str}"
                res = gov_session.get(url, timeout=5).json()
                if res.get("stat") == "OK" and "data" in res:
                    for row in res["data"]:
                        dp = row[0].split('/')
                        dt = pd.to_datetime(f"{int(dp[0])+1911}-{dp[1]}-{dp[2]}")
                        df_list.append({
                            "Date": dt,
                            "High": float(str(row[2]).replace(',', '').strip()),
                            "Low": float(str(row[3]).replace(',', '').strip()),
                            "Close": float(str(row[4]).replace(',', '').strip())
                        })
            else:
                # ⚡ 櫃買中心修正：精準修正為大盤主指數專用網址 /main/inid_result.php
                roc_ym = f"{d.year - 1911}/{d.strftime('%m')}"
                url = f"https://www.tpex.org.tw/zh-tw/indices/stock-index/main/inid_result.php?l=zh-tw&d={roc_ym}"
                res = gov_session.get(url, timeout=5).json()
                if res.get("stat") == "OK" and "aaData" in res:
                    for row in res["aaData"]:
                        if len(row) < 5: continue
                        dp = row[0].split('/')
                        if int(dp[0]) > 1900:
                            dt = pd.to_datetime(f"{dp[0]}-{dp[1]}-{dp[2]}")
                        else:
                            dt = pd.to_datetime(f"{int(dp[0])+1911}-{dp[1]}-{dp[2]}")
                        df_list.append({
                            "Date": dt,
                            "High": float(str(row[2]).replace(',', '').strip()),
                            "Low": float(str(row[3]).replace(',', '').strip()),
                            "Close": float(str(row[4]).replace(',', '').strip())
                        })
        except Exception as e:
            print(f"台灣官方歷史 API 獲取失敗 ({d}): {e}")

    if df_list:
        df = pd.DataFrame(df_list)
        df.set_index("Date", inplace=True)
        df = df[~df.index.duplicated(keep='last')]
        df.sort_index(inplace=True)
        return df
    return pd.DataFrame()

# ==========================================
# ⚡ 終極保險補丁：MiS 官方大盤即時報價串接
# ==========================================
def fetch_live_index_patch(is_tpex=False):
    """
    使用證交所 MiS 即時系統強制補齊今日最新的大盤K線數據。
    """
    try:
        ch = "otc_o00.tw" if is_tpex else "tse_t00.tw"
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ch}"
        res = gov_session.get(url, timeout=5).json()
        if "msgArray" in res and len(res["msgArray"]) > 0:
            info = res["msgArray"][0]
            if info.get("z") and info.get("d"):
                return {
                    "Date": pd.to_datetime(info["d"]),
                    "High": float(info["h"].replace(',', '')),
                    "Low": float(info["l"].replace(',', '')),
                    "Close": float(info["z"].replace(',', ''))
                }
    except Exception as e:
        print(f"⚠️ MiS 即時補丁連線失敗: {e}")
    return None

# ==========================================
# 📊 數據下載與關鍵價計算 (全面解鎖穩定版)
# ==========================================
def calculate_stock_prices(stock_id):
    try:
        days_back = 365
        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=1)
        start_date = today - datetime.timedelta(days=days_back)
        
        target = stock_id.upper().strip()
        is_tpex = False
        
        if target in ["TWII", "^TWII"]:
            yf_id = "^TWII"
            is_tw_index = True
            is_tw_stock = False
            is_tpex = False
        elif target in ["TWOII", "^TWOII"]:
            yf_id = "^TWOII"
            is_tw_index = True
            is_tw_stock = False
            is_tpex = True
        else:
            is_tw_index = False
            is_tw_stock = target.replace(".", "").isdigit() and len(target) >= 4
            yf_id = f"{target}.TW" if is_tw_stock else target

        print(f"--- 查詢代號確認: {yf_id} ---")

        df_daily = pd.DataFrame()

        # 🚀 路線一：大盤直連全新台灣官方歷史網址
        if is_tw_index:
            df_daily = get_taiwan_official_index(is_tpex)
            
        # 🚀 路線二：個股或大盤官方故障，走原生的 yfinance (移除強制覆寫的 session)
        if not is_tw_index or df_daily.empty:
            try:
                ticker_obj = yf.Ticker(yf_id)
                df_daily = ticker_obj.history(start=start_date, end=end_date, interval="1d")
                
                if is_tw_stock and df_daily.empty:
                    yf_id = f"{target}.TWO"
                    ticker_obj = yf.Ticker(yf_id)
                    df_daily = ticker_obj.history(start=start_date, end=end_date, interval="1d")
                    
                if not df_daily.empty:
                    if isinstance(df_daily.columns, pd.MultiIndex):
                        df_daily.columns = df_daily.columns.get_level_values(0)
                    df_daily.rename(columns=lambda x: x.capitalize(), inplace=True)
                    df_daily = df_daily[["High", "Low", "Close"]]
            except Exception:
                return None

        if df_daily.empty or len(df_daily) < 2:
            return None

        # 🚀 路線三：大盤即時補丁
        if is_tw_index:
            latest_date = df_daily.index[-1].date()
            if latest_date < today:
                live_bar = fetch_live_index_patch(is_tpex)
                if live_bar:
                    live_dt = live_bar["Date"]
                    df_daily.loc[live_dt] = [live_bar["High"], live_bar["Low"], live_bar["Close"]]
                    df_daily.sort_index(inplace=True)

        if pd.isna(df_daily.iloc[-1]["Close"]) or np.isnan(float(df_daily.iloc[-1]["Close"])):
            df_daily = df_daily.iloc[:-1]

        t_day = df_daily.iloc[-1]
        p_day = df_daily.iloc[-2]
        
        t_h, t_l, t_c = float(t_day["High"]), float(t_day["Low"]), float(t_day["Close"])
        p_h, p_l, p_c = float(p_day["High"]), float(p_day["Low"]), float(p_day["Close"])

        current_price = t_c
        yesterday_close = p_c
        quote_time = df_daily.index[-1].strftime("%Y-%m-%d")

        change_points = current_price - yesterday_close
        change_percent = (change_points / yesterday_close) * 100
        
        if change_points > 0:
            change_str = f"▲ {change_points:.2f} (+{change_percent:.2f}%)"
        elif change_points < 0:
            change_str = f"▼ {abs(change_points):.2f} (-{abs(change_percent):.2f}%)"
        else:
            change_str = f"─ 0.00 (0.00%)"

        t_res = t_h + (t_h - t_l) * 0.382
        t_key = (t_h + t_l) / 2
        t_sup = t_l - (t_h - t_l) * 0.382

        p_res = p_h + (p_h - p_l) * 0.382
        p_key = (p_h + p_l) / 2
        p_sup = p_l - (p_h - p_l) * 0.382

        df_weekly = df_daily.resample("W-FRI").agg({"High": "max", "Low": "min"}).dropna()
        w_key = float((df_weekly.iloc[-1]["High"] + df_weekly.iloc[-1]["Low"]) / 2)

        df_monthly = df_daily.resample("ME").agg({"High": "max", "Low": "min"}).dropna()
        m_key = float((df_monthly.iloc[-1]["High"] + df_monthly.iloc[-1]["Low"]) / 2)

        stock_name = ""
        if is_tw_index and not is_tpex:
            stock_name = "上市加權指數"
        elif is_tw_index and is_tpex:
            stock_name = "櫃買指數"
        elif is_tw_stock:
            try:
                tw_info = twstock.codes.get(target)
                if tw_info: stock_name = tw_info.name
            except Exception: pass
            
        if not stock_name:
            try:
                stock_name = yf.Ticker(yf_id).info.get("shortName", target)
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
        
        # ⚡ 依昨日關鍵價進行多空階層判斷 (精準文字版)
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
