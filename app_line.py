# ==========================================
# ✉️ LINE 訊息回覆傳送邏輯 (已刪除重覆的 report_text)
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
        status_week = "🔴 低於周關鍵價" if current < p['w_key'] else "🟢 站上周關鍵價"
        status_month = "🔴 低於月關鍵價" if current < p['m_key'] else "🟢 站上月關鍵價"

        # 乾淨漂亮的單一 report_text 定義
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
