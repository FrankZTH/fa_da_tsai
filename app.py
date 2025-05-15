import os
import sqlite3
import datetime
from flask import Flask, request, abort, jsonify

from linebot.v3.webhooks import WebhookParser
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import ReplyMessageRequest, TextMessage
from linebot.v3.webhooks.models import MessageEvent, TextMessageContent

app = Flask(__name__)

# 環境變數
channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
channel_secret = os.getenv("LINE_CHANNEL_SECRET")

configuration = Configuration(access_token=channel_access_token)
parser = WebhookParser(channel_secret)
messaging_api = MessagingApi(ApiClient(configuration))

# === 初始化資料庫 ===
def init_db():
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity
                 (user_id TEXT PRIMARY KEY,
                  display_name TEXT,
                  last_active TEXT)''')
    conn.commit()
    conn.close()

init_db()

# === 更新活躍用戶 ===
def update_user_activity(user_id, display_name):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    now = datetime.datetime.now().isoformat()
    c.execute("INSERT OR REPLACE INTO user_activity (user_id, display_name, last_active) VALUES (?, ?, ?)",
              (user_id, display_name, now))
    conn.commit()
    conn.close()

# === 查詢不活躍用戶 ===
def get_inactive_users(days=30):
    threshold = datetime.datetime.now() - datetime.timedelta(days=days)
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("SELECT display_name, last_active FROM user_activity")
    all_users = c.fetchall()
    conn.close()
    return [(name, ts) for name, ts in all_users if ts < threshold.isoformat()]

@app.route("/")
def home():
    return "LINE Bot is running."

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        print("Webhook parse error:", e)
        abort(400)

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            user_id = event.source.user_id
            group_id = getattr(event.source, "group_id", None)

            # 嘗試抓群組中的 display name
            display_name = "Unknown"
            try:
                if group_id:
                    profile = messaging_api.get_group_member_profile(group_id, user_id)
                    display_name = profile.display_name
                else:
                    profile = messaging_api.get_profile(user_id)
                    display_name = profile.display_name
            except Exception as e:
                print("Profile error:", e)

            update_user_activity(user_id, display_name)

            # 檢查訊息內容
            msg = event.message.text.lower()
            if msg == "查詢不活躍":
                inactive = get_inactive_users()
                if inactive:
                    reply = "\n".join([f"{name}（最後發言：{time[:10]}）" for name, time in inactive])
                else:
                    reply = "沒有發現不活躍的成員。"

                # 回覆訊息
                messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply)]
                    )
                )

    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
