import asyncio
import datetime
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yfinance as yf

# ==========================================
# ⚙️ 核心設定區
# ==========================================
# ⚠️ 請將下方引號內的文字，替換成 @BotFather 給你的 HTTP API Token
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"


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
# 🤖 機器人互動指令回覆邏輯
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 歡迎使用關鍵價看盤助手！\n\n"
        "💡 **使用方法：**\n"
        "直接在對話框輸入股票代號即可查詢。\n"
        "👉 例如輸入: `2330`、`8069` 或 `AAPL`"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_stock_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    status_message = await update.message.reply_text("🔍 正在大數據撈取與計算中，請稍候...")

    try:
        p = calculate_stock_prices(user_text)
        if p is None:
            await status_message.edit_text(f"❌ 找不到股票代號 '{user_text}' 的資料。")
            return

        report_text = (
            f"📊 **股票標的：{p['ticker_id']}**\n"
            f"🟧 **股票現價：{p['current']:.2f}**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 **【今日技術指標】**\n"
            f"🟥 今日壓力：`{p['t_res']:.2f}`\n"
            f"🟨 今日關鍵：`{p['t_key']:.2f}`\n"
            f"🟩 今日支撐：`{p['t_sup']:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ **【前日技術指標】**\n"
            f"🛑 前日壓力：`{p['p_res']:.2f}`\n"
            f"🪙 前日關鍵：`{p['p_key']:.2f}`\n"
            f"❇️ 前日支撐：`{p['p_sup']:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 **【長線波段參考】**\n"
            f"🔷 周關鍵價：`{p['w_key']:.2f}`\n"
            f"🔶 月關鍵價：`{p['m_key']:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💡 *提示：點擊數字可直接複製。*"
        )
        await status_message.edit_text(report_text, parse_mode="Markdown")

    except Exception as e:
        print(f"錯誤報告: {e}")
        await status_message.edit_text("❌ 系統計算時發生錯誤，請稍後再試。")


# ==========================================
# 🚀 啟動非同步監聽主程式 (繞過相容性 Bug)
# ==========================================
async def run_bot():
    print("🤖 關鍵價 Telegram 機器人正在啟動中...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_stock_query))

    # 核心替代方案：手動初始化與啟動更新機制，跳過 run_polling 的清理段落
    await app.initialize()
    await app.updater.start_polling()
    await app.start()
    
    print("🟢 機器人已成功上線！請打開 Telegram 開始測試。")
    
    # 讓程式保持運行
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        print("🛑 機器人正在關閉...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    # 執行非同步主迴圈
    asyncio.run(run_bot())
