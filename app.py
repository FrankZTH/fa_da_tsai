from flask import Flask, request, abort
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.webhooks import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging.models import ReplyMessageRequest, TextMessage
from linebot.exceptions import InvalidSignatureError
import os
import sqlite3
import datetime

app = Flask(__name__)

# 初始化 LINE SDK v3
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
messaging_api = MessagingApi(ApiClient(configuration))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

# 初始化資料庫
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

# 更新活躍時間
def update_user_activity(user_id, display_name):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    now = datetime.datetime.now().isoformat()
    c.execute("INSERT OR REPLACE INTO user_activity (user_id, display_name, last_active) VALUES (?, ?, ?)",
              (user_id, display_name, now))
    conn.commit()
    conn.close()

# 查詢不活躍用戶
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
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    if not isinstance(event.message, TextMessageContent):
        return

    user_id = event.source.user_id
    display_name = "Unknown"

    # 取得使用者名稱（群組或個人）
    try:
        if event.source.type == "group":
            group_id = event.source.group_id
            profile = messaging_api.get_group_member_profile(group_id, user_id)
        else:
            profile = messaging_api.get_profile(user_id)
        display_name = profile.display_name
    except Exception as e:
        print(f"無法取得使用者名稱：{e}")

    update_user_activity(user_id, display_name)

    msg = event.message.text.lower()
    if msg == "查詢不活躍":
        inactive = get_inactive_users()
        if inactive:
            reply = "\n".join([f"{name}（最後發言：{time[:10]}）" for name, time in inactive])
        else:
            reply = "沒有發現不活躍的成員。"

        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)