from flask import Flask, request, abort
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import ReplyMessageRequest, TextMessage
from linebot.v3.webhooks.models import MessageEvent, TextMessageContent, JoinEvent

import os
import sqlite3
import datetime

app = Flask(__name__)

# 初始化 LINE Messaging API
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
parser = WebhookParser(channel_secret=os.getenv("LINE_CHANNEL_SECRET"))

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

# 更新活躍紀錄
def update_user_activity(user_id, display_name):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    now = datetime.datetime.now().isoformat()
    c.execute("INSERT OR REPLACE INTO user_activity (user_id, display_name, last_active) VALUES (?, ?, ?)",
              (user_id, display_name, now))
    conn.commit()
    conn.close()

# 初始化群組成員
def init_group_members(group_id):
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        try:
            member_ids = messaging_api.get_group_member_ids(group_id)
            for member_id in member_ids.member_ids:
                try:
                    profile = messaging_api.get_group_member_profile(group_id, member_id)
                    update_user_activity(member_id, profile.display_name)
                except Exception as e:
                    print(f"[Error] Failed to get profile for {member_id}: {e}")
        except Exception as e:
            print(f"[Error] Failed to get group member IDs: {e}")

# 查詢不活躍成員
def get_inactive_users(seconds=3600):
    threshold = datetime.datetime.now() - datetime.timedelta(seconds=seconds)
    print("[Debug] Threshold =", threshold.isoformat())
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("SELECT display_name, last_active FROM user_activity")
    all_users = c.fetchall()
    print("[Debug] All users:", all_users)
    inactive = []
    for name, ts in all_users:
        try:
            last_active = datetime.datetime.fromisoformat(ts)
            print(f"[Debug] Comparing {name}: last_active={last_active}, threshold={threshold}")
            if last_active < threshold:
                inactive.append((name, ts))
        except ValueError as e:
            print(f"[Error] Invalid timestamp format for {name}: {ts}, error: {e}")
    print("[Debug] Inactive users:", inactive)
    conn.close()
    return inactive

@app.route("/")
def home():
    return "LINE Bot is running."

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        print("Webhook error:", e)
        abort(400)

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            handle_message(event)
        elif isinstance(event, JoinEvent) and event.source.type == "group":
            init_group_members(event.source.group_id)

    return "OK"

def handle_message(event):
    user_id = event.source.user_id
    display_name = "Unknown"

    try:
        with ApiClient(configuration) as api_client:
            messaging_api = MessagingApi(api_client)
            if event.source.type == "group":
                group_id = event.source.group_id
                profile = messaging_api.get_group_member_profile(group_id, user_id)
            elif event.source.type == "room":
                room_id = event.source.room_id
                profile = messaging_api.get_room_member_profile(room_id, user_id)
            else:
                profile = messaging_api.get_profile(user_id)
            display_name = profile.display_name
    except Exception as e:
        print("取得 profile 失敗：", e)

    update_user_activity(user_id, display_name)

    msg = event.message.text.lower()
    reply = None
    if msg == "查詢不活躍":
        inactive = get_inactive_users()
        if inactive:
            reply = "\n".join([f"{name}（最後發言：{ts[:19].replace('T', ' ')}）" for name, ts in inactive])
        else:
            reply = "沒有發現不活躍的成員。"
    elif msg == "初始化群組" and event.source.type == "group":
        init_group_members(event.source.group_id)
        reply = "已初始化群組成員資料。"

    if reply:
        with ApiClient(configuration)