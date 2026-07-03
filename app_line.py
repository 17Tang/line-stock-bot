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

# Yahoo 備用引擎防封鎖偽裝
yf_session = requests.Session()
yf_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})

# ==========================================
# 📊 數據下載與關鍵價計算 (大/小台期貨完美解鎖版)
# ==========================================
def calculate_stock_prices(stock_id):
    try:
        days_back = 365
        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=1)
        start_date = today - datetime.timedelta(days=days_back)
        
        target = stock_id.upper().strip()
        
        # 初始變數宣告
        finmind_id = target
        mis_ch = None
        display_name = target
        is_tw_stock = False
        is_futures = False
        
        # ⚡ 核心修正：精準建立大/小台期貨與大盤的分流導航系統
        if target in ["TX", "FITX", "台指期", "大台", "大台指"]:
            finmind_id = "TX"
            is_futures = True
            display_name = "TX 台指期近月連續"
        elif target in ["MTX", "FAMTX", "小台", "小台指"]:
            finmind_id = "MTX"
            is_futures = True
            display_name = "MTX 小台指近月連續"
        elif target in ["TWII", "^TWII", "TAIEX"]:
            finmind_id = "TAIEX"
            mis_ch = "tse_t00.tw"
            display_name = "^TWII 上市加權指數"
        elif target in ["TWOII", "^TWOII", "TWO"]:
            finmind_id = "TWO"
            mis_ch = "otc_o00.tw"
            display_name = "^TWOII 櫃買指數"
        else:
            is_tw_stock = target.replace(".", "").isdigit() and len(target) >= 4
            mis_ch = f"tse_{target}.tw"
            if is_tw_stock:
                try:
                    tw_info = twstock.codes.get(target)
                    if tw_info:
                        display_name = f"{target} {tw_info.name}"
                        mis_ch = f"otc_{target}.tw" if tw_info.market == "上櫃" else f"tse_{target}.tw"
                except Exception: pass

        print(f"--- 查詢核心啟動: {target} -> FinMind ID: {finmind_id} (期貨屬性: {is_futures}) ---")

        df_daily = pd.DataFrame()

        # 🚀 引擎 A：FinMind 全力運作
        try:
            if is_futures:
                # ⚡ 期貨專用無縫濾網
                url = "https://api.finmindtrade.com/api/v4/data"
                params = {"dataset": "TaiwanFuturesDaily", "data_id": finmind_id, "start_date": start_date.strftime("%Y-%m-%d")}
                res = requests.get(url, params=params, timeout=10).json()
                
                if res.get("msg") == "success" and res.get("data"):
                    df_raw = pd.DataFrame(res["data"])
                    df_raw['date'] = pd.to_datetime(df_raw['date'])
                    df_raw['volume'] = pd.to_numeric(df_raw['volume'], errors='coerce').fillna(0)
                    df_raw['contract_date'] = df_raw['contract_date'].astype(str).str.strip()
                    
                    # 過濾掉週合約，只保留長度為 6 的標準主力月合約 (例如 202607)
                    df_raw = df_raw[df_raw['contract_date'].str.len() == 6]
                    
                    if not df_raw.empty:
                        # ⚡ 終極防禦改動：用先排序再降維去重法，100% 抓出每日成交量最大的近月主力合約，絕不崩潰
                        df_raw = df_raw.sort_values(by=['date', 'volume'], ascending=[True, False])
                        df_near = df_raw.drop_duplicates(subset=['date'], keep='first').copy()
                        df_near.set_index('date', inplace=True)
                        
                        df_daily = pd.DataFrame(index=df_near.index)
                        df_daily['High'] = df_near['max'].astype(float)
                        df_daily['Low'] = df_near['min'].astype(float)
                        df_daily['Close'] = df_near['close'].astype(float)
            else:
                # 股票與現貨大盤通道
                url = "https://api.finmindtrade.com/api/v4/data"
                params = {"dataset": "TaiwanStockPrice", "data_id": finmind_id, "start_date": start_date.strftime("%Y-%m-%d")}
                res = requests.get(url, params=params, timeout=10).json()
                if res.get("msg") == "success" and res.get("data"):
                    df_raw = pd.DataFrame(res["data"])
                    df_raw['date'] = pd.to_datetime(df_raw['date'])
                    df_raw.set_index('date', inplace=True)
                    df_daily = pd.DataFrame(index=df_raw.index)
                    df_daily['High'] = df_raw['max'].astype(float)
                    df_daily['Low'] = df_raw['min'].astype(float)
                    df_daily['Close'] = df_raw['close'].astype(float)
        except Exception as e:
            print(f"FinMind 數據庫讀取發生阻礙: {e}")

        # 🚀 引擎 B：動態對沖分流 (當 FinMind 查無資料時，由 Yahoo 完美無縫接管)
        if df_daily.empty:
            print("🔄 FinMind 未能成功輸出，立即調度 Yahoo Finance 備用引擎...")
            try:
                if is_futures:
                    yf_id = "TAIE=F" # Yahoo 上的台指期主力連續代號
                elif target in ["TWII", "^TWII", "TAIEX"]:
                    yf_id = "^TWII"
                elif target in ["TWOII", "^TWOII", "TWO"]:
                    yf_id = "^TWOII"
                else:
                    yf_id = f"{target}.TW" if is_tw_stock else target
                    
                df_yf = yf.download(yf_id, start=start_date, end=end_date, progress=False, session=yf_session)
                
                if is_tw_stock and df_yf.empty:
                    yf_id = f"{target}.TWO"
                    df_yf = yf.download(yf_id, start=start_date, end=end_date, progress=False, session=yf_session)
                    mis_ch = f"otc_{target}.tw"
                    
                if not df_yf.empty:
                    if isinstance(df_yf.columns, pd.MultiIndex):
                        df_yf.columns = df_yf.columns.get_level_values(0)
                    df_yf.rename(columns=lambda x: x.capitalize(), inplace=True)
                    df_daily = df_yf[["High", "Low", "Close"]].copy()
            except Exception as yfe:
                print(f"警告：Yahoo 備用引擎連線失敗: {yfe}")
                return None

        if df_daily.empty or len(df_daily) < 2:
            return None

        mis_yesterday_close = None

        # 🚀 路線三：MiS 官方現貨即時補丁 (期貨不走證交所個股通道)
        if mis_ch:
            try:
                latest_date = df_daily.index[-1].date()
                if latest_date < today:
                    print(f"⚠️ 歷史數據日期落後，強行載入 MiS 盤中即時補丁...")
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
                            
                            if info.get("y") and info["y"] != "-":
                                mis_yesterday_close = float(info["y"].replace(',', ''))
                            
                            df_daily.loc[live_dt] = [live_high, live_low, live_close]
                            df_daily = df_daily[~df_daily.index.duplicated(keep='last')]
                            df_daily.sort_index(inplace=True)
                            print(f"🟢 成功補齊今日 MiS 數據！")
            except Exception as e:
                print(f"⚠️ MiS 即時補丁無回應: {e}")

        # 僅檢查 NaN 空棒
        if pd.isna(df_daily.iloc[-1]["Close"]) or np.isnan(float(df_daily.iloc[-1]["Close"])):
            df_daily = df_daily.iloc[:-1]

        t_day = df_daily.iloc[-1]
        p_day = df_daily.iloc[-2]
        
        t_h, t_l, t_c = float(t_day["High"]), float(t_day["Low"]), float(t_day["Close"])
        p_h, p_l, p_c = float(p_day["High"]), float(p_day["Low"]), float(p_day["Close"])

        current_price = t_c
        yesterday_close = mis_yesterday_close if mis_yesterday_close is not None else p_c
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
        send_line_reply(reply_token, "歡迎使用關鍵價看盤助手！\n\n請在股號前加一個『#』即可查詢。\n例如輸入：#2330、#TWOII、#TX 或 #MTX")
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
        
        # 依昨日關鍵價進行多空階層判斷
        if current < p['p_sup']:
            status_yesterday = "🚨 跌破多防價 極度空頭"
        elif current < p['p_key']:
            status_yesterday = "🟡 小於關鍵價 未破多防"
        elif current <= p['p_res']:
            status_yesterday = "🔵 大於關鍵價 未達空防"
        else:
            status_yesterday = "🔥 漲過空防價 強勢多頭"

        # 判斷是否大於周、月關鍵價
        status_week = "🟢 低於周關鍵價" if current < p['w_key'] else "🔴 站上周關鍵價"
        status_month = "🟢 低於月關鍵價" if current < p['m_key'] else "🔴 站上月關鍵價"

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
