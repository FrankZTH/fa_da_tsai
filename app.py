from flask import Flask, request, abort
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import ReplyMessageRequest, TextMessage
from linebot.v3.webhooks.models import MessageEvent, TextMessageContent, JoinEvent, MemberJoinedEvent, MemberLeftEvent

import os
import sqlite3
import datetime
import time

app = Flask(__name__)

# 初始化 LINE Messaging API
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
parser = WebhookParser(channel_secret=os.getenv("LINE_CHANNEL_SECRET"))

# 初始化資料庫
def init_db():
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity
                 (user_id TEXT,
                  group_id TEXT,
                  display_name TEXT,
                  last_active TEXT,
                  PRIMARY KEY (user_id, group_id))''')
    conn.commit()
    conn.close()

init_db()

# 更新活躍紀錄
def update_user_activity(user_id, display_name, group_id, update_time=True):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    last_active = datetime.datetime.now().isoformat() if update_time else None
    c.execute("INSERT OR REPLACE INTO user_activity (user_id, group_id, display_name, last_active) VALUES (?, ?, ?, ?)",
              (user_id, group_id, display_name, last_active))
    conn.commit()
    conn.close()
    print(f"[Debug] Updated user: {user_id}, group: {group_id}, name: {display_name}, last_active: {last_active}")

# 移除離開的成員
def remove_user(user_id, group_id):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("DELETE FROM user_activity WHERE user_id = ? AND group_id = ?", (user_id, group_id))
    conn.commit()
    conn.close()
    print(f"[Debug] Removed user: {user_id} from group: {group_id}")

# 初始化群組成員
def init_group_members(group_id):
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        try:
            member_ids = messaging_api.get_group_member_ids(group_id)
            count = 0
            for member_id in member_ids.member_ids:
                try:
                    profile = messaging_api.get_group_member_profile(group_id, member_id)
                    update_user_activity(member_id, profile.display_name, group_id, update_time=False)  # 初始化時不更新時間
                    count += 1
                    time.sleep(0.1)  # 避免 API 頻率限制
                except Exception as e:
                    print(f"[Error] Failed to get profile for {member_id}: {e}")
            print(f"[Debug] Initialized {count} members in group {group_id}")
            return count
        except Exception as e:
            print(f"[Error] Failed to get group member IDs for group {group_id}: {e}")
            return 0

# 查詢不活躍成員
def get_inactive_users(group_id, seconds=3600):
    threshold = datetime.datetime.now() - datetime.timedelta(seconds=seconds)
    print(f"[Debug] Threshold for group {group_id}: {threshold.isoformat()}")
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("SELECT display_name, last_active FROM user_activity WHERE group_id = ? AND last_active IS NOT NULL", (group_id,))
    all_users = c.fetchall()
    print(f"[Debug] All users in group {group_id}: {all_users}")
    inactive = []
    for name, ts in all_users:
        try:
            last_active = datetime.datetime.fromisoformat(ts)
            print(f"[Debug] Comparing {name}: last_active={last_active}, threshold={threshold}")
            if last_active < threshold:
                inactive.append((name, ts))
        except ValueError as e:
            print(f"[Error] Invalid timestamp format for {name}: {ts}, error: {e}")
    print(f"[Debug] Inactive users in group {group_id}: {inactive}")
    conn.close()
    return inactive

# 檢查群組成員數量
def get_member_count(group_id):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM user_activity WHERE group_id = ?", (group_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

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
        print(f"[Webhook Error]: {e}")
        abort(400)

    for event in events:
        print(f"[Debug] Event received: {event.__class__.__name__}")
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            handle_message(event)
        elif isinstance(event, JoinEvent) and event.source.type == "group":
            print(f"[Debug] JoinEvent for group: {event.source.group_id}")
            init_group_members(event.source.group_id)
        elif isinstance(event, MemberJoinedEvent) and event.source.type == "group":
            handle_member_joined(event)
        elif isinstance(event, MemberLeftEvent) and event.source.type == "group":
            handle_member_left(event)

    return "OK"

def handle_message(event):
    user_id = event.source.user_id
    display_name = "Unknown"
    group_id = event.source.group_id if event.source.type == "group" else None

    try:
        with ApiClient(configuration) as api_client:
            messaging_api = MessagingApi(api_client)
            if event.source.type == "group":
                profile = messaging_api.get_group_member_profile(group_id, user_id)
            elif event.source.type == "room":
                room_id = event.source.room_id
                profile = messaging_api.get_room_member_profile(room_id, user_id)
            else:
                profile = messaging_api.get_profile(user_id)
            display_name = profile.display_name
    except Exception as e:
        print(f"[Error] Failed to get profile for {user_id}: {e}")

    if group_id:
        update_user_activity(user_id, display_name, group_id)

    msg = event.message.text.lower()
    reply = None
    if msg == "查詢不活躍" and group_id:
        member_count = get_member_count(group_id)
        if member_count == 0:
            init_group_members(group_id)  # 如果資料庫中無記錄，自動初始化
            member_count = get_member_count(group_id)
        inactive = get_inactive_users(group_id)
        if inactive:
            reply = "\n".join([f"{name}（最後發言：{ts[:19].replace('T', ' ')}）" for name, ts in inactive])
        else:
            reply = f"群組內無不活躍成員（已記錄 {member_count} 位成員）。"
    elif msg == "初始化群組" and group_id:
        count = init_group_members(group_id)
        reply = f"已初始化群組，記錄 {count} 位成員。"
    elif msg == "檢查成員數" and group_id:
        count = get_member_count(group_id)
        reply = f"資料庫中記錄了 {count} 位群組成員。"

    if reply:
        with ApiClient(configuration) as api_client:
            messaging_api = MessagingApi(api_client)
            try:
                messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply)]
                    )
                )
            except Exception as e:
                print(f"[Error] Failed to send reply: {e}")

def handle_member_joined(event):
    group_id = event.source.group_id
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        for member in event.joined.members:
            user_id = member.user_id
            try:
                profile = messaging_api.get_group_member_profile(group_id, user_id)
                update_user_activity(user_id, profile.display_name, group_id, update_time=False)
                print(f"[Debug] New member joined: {user_id} in group {group_id}")
            except Exception as e:
                print(f"[Error] Failed to get profile for {user_id}: {e}")

def handle_member_left(event):
    group_id = event.source.group_id
    for member in event.left.members:
        user_id = member.user_id
        remove_user(user_id, group_id)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)