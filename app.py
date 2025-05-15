from flask import Flask, request, abort
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import ReplyMessageRequest, TextMessage
from linebot.v3.webhooks.models import MessageEvent, TextMessageContent, JoinEvent, MemberJoinedEvent, MemberLeftEvent

import os
import sqlite3
import datetime
import time
import requests

app = Flask(__name__)

# 初始化 LINE Messaging API
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
parser = WebhookParser(channel_secret=os.getenv("LINE_CHANNEL_SECRET"))

# Bot 自身的 user_id（需在 LINE Developer Console 或日誌中獲取）
BOT_USER_ID = os.getenv("BOT_USER_ID", "YOUR_BOT_USER_ID")  # 請設置 Bot 的 user_id

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
    if user_id == BOT_USER_ID:
        print(f"[Debug] Skipping Bot user_id: {user_id}")
        return
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

# 初始化群組成員（帶重試機制）
def init_group_members(group_id, retries=3, delay=1):
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        for attempt in range(retries):
            try:
                member_ids = messaging_api.get_group_member_ids(group_id)
                print(f"[Debug] Retrieved {len(member_ids.member_ids)} member IDs for group {group_id}: {member_ids.member_ids}")
                count = 0
                for member_id in member_ids.member_ids:
                    if member_id == BOT_USER_ID:
                        print(f"[Debug] Skipping Bot user_id: {member_id}")
                        continue
                    for profile_attempt in range(retries):
                        try:
                            profile = messaging_api.get_group_member_profile(group_id, member_id)
                            update_user_activity(member_id, profile.display_name, group_id, update_time=False)
                            count += 1
                            time.sleep(0.5)  # 增加延遲
                            break
                        except Exception as e:
                            print(f"[Error] Attempt {profile_attempt + 1} failed to get profile for {member_id} in group {group_id}: {e}")
                            if profile_attempt < retries - 1:
                                time.sleep(delay)
                            else:
                                print(f"[Error] Skipped {member_id} after {retries} attempts")
                print(f"[Debug] Initialized {count} members in group {group_id}")
                return count
            except Exception as e:
                print(f"[Error] Attempt {attempt + 1} failed to get group member IDs for group {group_id}: {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
        print(f"[Error] Failed to initialize group {group_id} after {retries} attempts")
        return 0

# 查詢不活躍成員
def get_inactive_users(group_id, days=7):
    threshold = datetime.datetime.now() - datetime.timedelta(days=days)
    print(f"[Debug] Threshold for group {group_id}: {threshold.isoformat()}")
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("SELECT display_name, last_active FROM user_activity WHERE group_id = ?", (group_id,))
    all_users = c.fetchall()
    print(f"[Debug] All users in group {group_id}: {all_users}")
    inactive = []
    for name, ts in all_users:
        if ts is None:
            inactive.append((name, "尚未發言"))
        else:
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

# 檢查資料庫內容
def get_group_members(group_id):
    conn = sqlite3.connect("user_tracker.db")
    c = conn.cursor()
    c.execute("SELECT user_id, display_name, last_active FROM user_activity WHERE group_id = ?", (group_id,))
    members = c.fetchall()
    conn.close()
    return members

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
        print(f"[Debug] Event received: {event.__class__.__name__}, source: {event.source}")
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
                profile = messaging_api.get_room_member_profile(event.source.room_id, user_id)
            else:
                profile = messaging_api.get_profile(user_id)
            display_name = profile.display_name
    except Exception as e:
        print(f"[Error] Failed to get profile for {user_id} in group {group_id}: {e}")

    if group_id:
        conn = sqlite3.connect("user_tracker.db")
        c = conn.cursor()
        c.execute("SELECT 1 FROM user_activity WHERE user_id = ? AND group_id = ?", (user_id, group_id))
        if not c.fetchone():
            update_user_activity(user_id, display_name, group_id, update_time=False)
            print(f"[Debug] Added missing user {user_id} to group {group_id}")
        else:
            update_user_activity(user_id, display_name, group_id)
        conn.close()

    msg = event.message.text.lower()
    reply = None
    if msg == "查詢不活躍" and group_id:
        member_count = get_member_count(group_id)
        if member_count < 2:  # 假設群組至少有 2 人
            init_group_members(group_id)
            member_count = get_member_count(group_id)
        inactive = get_inactive_users(group_id)
        if inactive:
            reply = "\n".join([f"{name}（{ts if ts == '尚未發言' else ts[:19].replace('T', ' ')}）" for name, ts in inactive[:10]])
            if len(inactive) > 10:
                reply += f"\n...還有 {len(inactive) - 10} 位不活躍成員"
        else:
            reply = f"群組內無不活躍成員（已記錄 {member_count} 位成員）。"
    elif msg == "初始化群組" and group_id:
        count = init_group_members(group_id)
        reply = f"已初始化群組，記錄 {count} 位成員。"
    elif msg == "檢查成員數" and group_id:
        count = get_member_count(group_id)
        reply = f"資料庫中記錄了 {count} 位群組成員。"
    elif msg == "檢查資料庫" and group_id:
        members = get_group_members(group_id)
        if members:
            reply = "\n".join([f"ID: {user_id}, Name: {name}, Last Active: {last_active if last_active else '尚未發言'}" for user_id, name, last_active in members])
        else:
            reply = "資料庫中無此群組成員記錄。"
            init_group_members(group_id)

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
                print(f"[Error] Failed to send reply for group {group_id}: {e}")

def handle_member_joined(event):
    group_id = event.source.group_id
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        for member in event.joined.members:
            user_id = member.user_id
            if user_id == BOT_USER_ID:
                print(f"[Debug] Skipping Bot user_id: {user_id}")
                continue
            for attempt in range(3):
                try:
                    profile = messaging_api.get_group_member_profile(group_id, user_id)
                    update_user_activity(user_id, profile.display_name, group_id, update_time=False)
                    print(f"[Debug] New member joined: {user_id} in group {group_id}")
                    break
                except Exception as e:
                    print(f"[Error] Attempt {attempt + 1} failed to get profile for {user_id} in group {group_id}: {e}")
                    if attempt < 2:
                        time.sleep(1)

def handle_member_left(event):
    group_id = event.source.group_id
    for member in event.left.members:
        user_id = member.user_id
        remove_user(user_id, group_id)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)