# -*- coding: utf-8 -*-
"""
Virtual Number Shop - Telegram Bot
====================================================================
বাটন, মেনু নেভিগেশন ও স্ট্যাকচারের পাশাপাশি এখন Purchase (Buy one pcs / Bulk Buy)
এবং Admin File Upload (stock যুক্ত করা) এর real logic যুক্ত করা হয়েছে।
✅ ডেটা এখন আর শুধু in-memory (RAM) তে থাকে না — সব ডেটা (users/products/
orders/deposits/sms_log/bot_settings) অটোমেটিক ডিস্কে JSON ফাইলে (DB_FILE_PATH,
ডিফল্ট: bot_data.json) সেভ হয় এবং bot চালু হওয়ার সময় সেখান থেকে লোড হয়ে যায়,
তাই bot restart হলেও ডেটা হারায় না (দেখুন: save_db() / load_db())। Railway তে
এটা সত্যিকারের persistent রাখতে হলে DB_FILE_PATH একটা attached Volume এর
path এ সেট করে দিন, নাহলে নতুন ডিপ্লয়ে ফাইল রিসেট হয়ে যেতে পারে।
যেখানে আসল Payment gateway বসবে সেখানে এখনও # TODO: ... কমেন্ট দিয়ে চিহ্নিত
করা আছে।

Library: pyTelegramBotAPI (telebot)
    pip install pyTelegramBotAPI flask openpyxl

Environment variables (Railway এ / .env এ সেট করবেন):
    BOT_TOKEN     -> BotFather থেকে পাওয়া টোকেন
    ADMIN_ID      -> আপনার টেলিগ্রাম User ID (একাধিক হলে কমা দিয়ে আলাদা করুন)
    RAILWAY_URL   -> Railway তে ডিপ্লয় করা অ্যাপের পাবলিক URL (webhook এর জন্য)
    PORT          -> Railway যে পোর্ট দেয় (ডিফল্ট 8080)
    DB_FILE_PATH  -> persistent DB JSON ফাইলের path (ডিফল্ট: স্ক্রিপ্টের পাশে bot_data.json)

Run mode:
    - RAILWAY_URL সেট থাকলে -> Webhook mode (Flask + Railway)
    - RAILWAY_URL না থাকলে -> Polling mode (লোকাল টেস্টিং এর জন্য)
"""

import os
import io
import json
import re
import threading
import time
import uuid
import datetime

import telebot
from telebot import types

# ---------------------------------------------------------------------------
# ENV / CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_ID", "6053411200").split(",")
    if x.strip().isdigit()
]
RAILWAY_URL = os.environ.get("RAILWAY_URL", "")   # e.g. https://your-app.up.railway.app
PORT = int(os.environ.get("PORT", 8080))
REFERRAL_BONUS = 10   # TODO: real bonus amount (এডমিন Bot Settings থেকে সেট করার আগে placeholder)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# বটের আসল username নিজে থেকে (dynamically) নিয়ে নেওয়া হচ্ছে, যাতে রেফারেল লিংকে
# ভুল/হার্ডকোড করা username (যেমন "vertual_shop_bot" / "virtualshop") না বসে।
try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username")  # fallback

# ---------------------------------------------------------------------------
# IN-MEMORY "DATABASE" (এখন অটোমেটিক ডিস্কে JSON ফাইলে persist হয়, দেখুন save_db()/
# load_db()/start_persistent_db() — নিচের দিকে "PERSISTENT DATABASE" সেকশনে)
# ---------------------------------------------------------------------------
users = {}      # user_id -> {full_name, username, balance, total_purchased, today_spent, today_deposit, referrals, earned}
products = {
    "whatsapp": {
        "name": "WhatsApp Number",
        "price": 0,          # TODO: real price (এডমিন সেট করার আগে purchase ব্লক থাকবে)
        "stock": 0,          # Upload File থেকে auto আপডেট হয়
        "description": "WhatsApp verification number.",  # TODO
        "stock_list": [],    # [{"number": "...", "otp_link": "..."}, ...]
    },
    "telegram": {
        "name": "Telegram Number",
        "price": 0,          # TODO: real price (এডমিন সেট করার আগে purchase ব্লক থাকবে)
        "stock": 0,          # Upload File থেকে auto আপডেট হয়
        "description": "Telegram verification number.",  # TODO
        "stock_list": [],    # [{"number": "...", "otp_link": "..."}, ...]
    },
}
orders = {}     # order_id -> order data
deposits = {}   # deposit_id -> {id, user_id, method, amount, trx_id, status, date}
_deposit_id_counter = [1000]   # পরবর্তী deposit id বানানোর কাউন্টার (in-memory)
# 🔒 একই Deposit যেন দুইজন Admin (বা একজন Admin ডাবল-ক্লিক করে) প্রায় একই সময়ে
# Approve/Reject করলে দুইবার ব্যালেন্স যোগ না হয়ে যায় (race condition), সেজন্য
# "status pending কিনা চেক করা -> approved/rejected এ সেট করা -> ব্যালেন্স যোগ করা"
# পুরো অংশটুকু সবসময় এই লক দিয়ে atomic রাখা হয় (ম্যানুয়াল Approve/Reject এবং
# SMS auto-approve/auto-reject — দুই জায়গাতেই)।
_deposit_lock = threading.Lock()
# 🔒 দুইজন ইউজার প্রায় একই সময়ে একই প্রোডাক্ট কিনতে গেলে (বিশেষত স্টক ১টা থাকা
# অবস্থায়), "স্টক আছে কিনা চেক করা -> pop করে item বের করা -> balance কাটা ->
# order সেভ করা" পুরো অংশটুকু atomic রাখতে এই লক ব্যবহার হয় (Buy one pcs ও
# Bulk Buy দুই জায়গাতেই), যাতে stock check আর pop এর মাঝে race condition
# (IndexError crash বা ভুল স্টক গণনা) না হয়।
_stock_lock = threading.Lock()
sms_log = []    # SMS Forwarder থেকে আসা প্রতিটা পার্স-করা পেমেন্ট SMS/নোটিফিকেশন
_sms_id_counter = [0]          # পরবর্তী sms log id বানানোর কাউন্টার (in-memory)


def _next_deposit_id():
    _deposit_id_counter[0] += 1
    return _deposit_id_counter[0]


def _next_sms_id():
    _sms_id_counter[0] += 1
    return _sms_id_counter[0]


# এই মেথডগুলোর ডিপোজিট সবসময় ম্যানুয়ালি Admin রিভিউ করে Approve/Reject করা হবে
# (bKash/Nagad/Rocket/Binance -> TrxID যাচাই করে Admin নিজে Approve করবে)।
# bot_settings["deposit_methods"] এ এর বাইরে যেকোনো মেথড থাকলে সেটা সাথে সাথে
# (কোনো Admin রিভিউ ছাড়াই) অটো-অ্যাপ্রুভ হয়ে যাবে।
DEPOSIT_MANUAL_METHODS = ["bKash", "Nagad", "Rocket", "Binance"]

# এই কয়টা মেথডেই শুধু SMS Forwarder App থেকে আসা SMS এর সাথে TrxID/Amount মিলিয়ে
# অটো-অ্যাপ্রুভ করা হয় (SMS আসে বলে)। Binance এর কোনো SMS আসে না, তাই সেটা এই
# তালিকায় নেই — Binance সবসময় pending থেকে Admin ম্যানুয়ালি Approve/Reject করবে।
SMS_AUTO_APPROVE_METHODS = ["bKash", "Nagad", "Rocket"]

# ---------------------------------------------------------------------------
# BOT SETTINGS (runtime-editable, Admin Panel -> ⚙️ Bot Settings থেকে বদলানো যায়,
# প্রতিটা পরিবর্তনের পর save_db() কল হয়ে ডিস্কে persist হয়ে যায়)
# ---------------------------------------------------------------------------
bot_settings = {
    "referral_bonus": REFERRAL_BONUS,   # প্রতি রেফারেলে বোনাস (BDT)
    "min_deposit": 0,                   # 0 মানে কোনো সীমা সেট করা নেই
    "max_deposit": 0,                   # 0 মানে কোনো সীমা সেট করা নেই
    "maintenance_mode": False,          # True হলে সাধারণ ইউজাররা বট ব্যবহার করতে পারবে না
    "deposit_methods": ["bKash", "Nagad", "Rocket", "Binance"],  # কমা-আলাদা তালিকা, Admin থেকে এডিট হয়
    "deposit_numbers": {                # প্রতিটা মেথডের পেমেন্ট নাম্বার/অ্যাড্রেস (Admin প্যানেল থেকে সেট হবে)
        "bKash": "",
        "Nagad": "",
        "Rocket": "",
        "Binance": "",
    },
    "usd_rate": 0,                       # 1 USD = কত BDT (Admin Panel থেকে সেট হবে; 0 মানে সেট করা নেই)
    "support_username": "",              # 🆘 Support বাটনের "24/7 live chat" এর জন্য (@ ছাড়া বা সহ, দুটোই চলবে)
    "method_videos": [],                 # ⚙️ Method বাটনে দেখানো টিউটোরিয়াল ভিডিওর তালিকা: [{"title": ..., "link": ...}, ...]
    "force_join_channels": [],           # 🔐 Force Join চ্যানেল/গ্রুপের তালিকা: [{"name": ..., "link": ..., "chat_id": ...}, ...]
}

# navigation state per user, e.g. {"menu": "buy_number", "product": "whatsapp"}
user_state = {}


def get_user(message_or_call):
    """Ensure user exists in temp store and return the user dict."""
    u = message_or_call.from_user
    uid = u.id
    if uid not in users:
        users[uid] = {
            "full_name": (u.first_name or "") + ((" " + u.last_name) if u.last_name else ""),
            "username": f"@{u.username}" if u.username else "N/A",
            "balance": 0,
            "total_purchased": 0,
            "today_spent": 0,
            "today_deposit": 0,
            "referrals": 0,
            "earned": 0,
            "referred_by": None,   # কে রেফার করেছে (user_id), একবারই সেট হবে
        }
    return users[uid]


def is_admin(user_id):
    return user_id in ADMIN_IDS


def maintenance_block(ctx):
    """Maintenance mode চালু থাকলে non-admin ইউজারদের ব্লক করে True রিটার্ন করে।
    ctx একটি message অথবা callback হতে পারে।"""
    uid = ctx.from_user.id
    if bot_settings["maintenance_mode"] and not is_admin(uid):
        chat_id = ctx.chat.id if hasattr(ctx, "chat") else ctx.message.chat.id
        bot.send_message(
            chat_id,
            "🛠️ <b>Bot Update চলতেছে</b>\n\n"
            "সাময়িক অসুবিধার জন্য দুঃখিত। একটু পরে আবার চেষ্টা করুন।",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# FORCE JOIN (Admin Panel -> 🔐 Force Join থেকে এডমিন চ্যানেল/গ্রুপ যুক্ত করবে,
# ইউজার প্রথমবার /start দিলে সেগুলোতে জয়েন করতে বলা হবে, জয়েন না করা পর্যন্ত
# বট ব্যবহার করতে পারবে না)
# ---------------------------------------------------------------------------
def get_force_join_channels():
    return bot_settings.get("force_join_channels", [])


def check_user_joined_all(user_id):
    """সব ফোর্স-জয়েন চ্যানেল/গ্রুপে ইউজার জয়েন করেছে কিনা লাইভ চেক করে (Telegram API
    দিয়ে)। বট নিজে ঐ চ্যানেল/গ্রুপে Admin হিসেবে না থাকলে get_chat_member এরর দিতে
    পারে — নিরাপত্তার জন্য সেক্ষেত্রে not-joined (False) ধরে নেওয়া হয়।"""
    channels = get_force_join_channels()
    if not channels:
        return True
    for ch in channels:
        cid = ch.get("chat_id")
        if not cid:
            continue
        try:
            member = bot.get_chat_member(cid, user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            return False
    return True


def force_join_prompt_inline():
    """ইউজারকে দেখানো জয়েন বাটন গুলো + ✅ Join Check বাটন।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in get_force_join_channels():
        title = ch.get("name") or "Channel"
        link = ch.get("link")
        if link:
            kb.add(types.InlineKeyboardButton(f"📢 {title}", url=link))
    kb.add(types.InlineKeyboardButton("✅ Join Check", callback_data="check_join"))
    return kb


def send_force_join_prompt(chat_id):
    bot.send_message(
        chat_id,
        "🔐 <b>বট ব্যবহার করার আগে জয়েন করুন</b>\n\n"
        "নিচের চ্যানেল/গ্রুপ(গুলো)-তে জয়েন করুন, তারপর 👇 ✅ Join Check বাটনে ক্লিক করুন।",
        reply_markup=force_join_prompt_inline(),
    )


def require_force_join(ctx):
    """Force Join গেট। ইউজার এখনও সব চ্যানেল/গ্রুপে জয়েন না করে থাকলে জয়েন
    প্রম্পট পাঠিয়ে True (blocked) রিটার্ন করে; জয়েন থাকলে/এডমিন হলে/কোনো ফোর্স
    জয়েন চ্যানেল সেট করা না থাকলে False (not blocked) রিটার্ন করে। ctx একটি
    message অথবা callback হতে পারে।"""
    uid = ctx.from_user.id
    if is_admin(uid):
        return False
    if not get_force_join_channels():
        return False

    u = users.get(uid)
    if u and u.get("force_join_passed"):
        return False

    chat_id = ctx.chat.id if hasattr(ctx, "chat") else ctx.message.chat.id
    if check_user_joined_all(uid):
        if u:
            u["force_join_passed"] = True
            save_db()
        return False

    user_state[uid] = {"menu": "force_join_wait"}
    send_force_join_prompt(chat_id)
    return True


def force_join_admin_inline():
    """Admin Panel -> 🔐 Force Join সাব-মেনু: বর্তমানে যুক্ত চ্যানেল/গ্রুপ
    (Remove বাটনসহ) + নতুন যুক্ত করার বাটন।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    channels = get_force_join_channels()
    if not channels:
        kb.add(types.InlineKeyboardButton("❌ কোনো চ্যানেল/গ্রুপ যুক্ত নেই", callback_data="fj_noop"))
    else:
        for i, ch in enumerate(channels):
            title = ch.get("name") or "Channel"
            kb.add(types.InlineKeyboardButton(f"🗑️ Remove: {title}", callback_data=f"fj_remove_{i}"))
    kb.add(types.InlineKeyboardButton("➕ Add Channel/Group", callback_data="fj_add"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="fj_back"))
    return kb


def method_video_admin_inline():
    """⚙️ Method এর টিউটোরিয়াল ভিডিও বাটন ম্যানেজ করার সাব-মেনু: বর্তমান
    ভিডিও বাটনগুলো (Remove সহ) + নতুন যুক্ত করার বাটন।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    videos = bot_settings.get("method_videos") or []
    if not videos:
        kb.add(types.InlineKeyboardButton("❌ কোনো ভিডিও বাটন যুক্ত নেই", callback_data="mv_noop"))
    else:
        for i, v in enumerate(videos):
            kb.add(types.InlineKeyboardButton(f"🗑️ Remove: {v['title']}", callback_data=f"mv_remove_{i}"))
    kb.add(types.InlineKeyboardButton("➕ Add New Video", callback_data="mv_add"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="mv_back"))
    return kb


def fmt_amount(value):
    """সব জায়গায় একই ফরম্যাটে টাকা দেখানোর জন্য: 100৳ ($0.91)
    bot_settings["usd_rate"] (1 USD = কত BDT) অনুযায়ী ডলার হিসাব করা হয়;
    রেট সেট করা না থাকলে (0) BDT ভ্যালুটাই ডলার হিসেবে দেখানো হয়।"""
    rate = bot_settings.get("usd_rate") or 0
    usd = (value / rate) if rate > 0 else value
    return f"{value}৳ (${usd:.2f})"


def safe_edit_or_send(chat_id, msg_id, text, reply_markup=None):
    """একটামাত্র মেসেজ এডিট করে ধাপে ধাপে ফ্লো দেখানোর জন্য কমন হেল্পার
    (যেমন Deposit ফ্লো)। msg_id থাকলে সেই মেসেজটা এডিট করার চেষ্টা করে;
    এডিট ফেইল করলে (৪৮ ঘণ্টা পার হয়ে গেছে / মেসেজ ডিলিট হয়ে গেছে ইত্যাদি)
    fallback হিসেবে নতুন মেসেজ পাঠায়। সবসময় (edit হোক বা নতুন পাঠানো হোক)
    বর্তমান "অ্যাংকর" মেসেজের message_id রিটার্ন করে, যাতে পরের ধাপেও সেটাই
    এডিট করা যায়।"""
    if msg_id:
        try:
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=reply_markup,
            )
            return msg_id
        except Exception:
            pass
    sent = bot.send_message(chat_id, text, reply_markup=reply_markup)
    return sent.message_id


# ---------------------------------------------------------------------------
# KEYBOARDS (Reply / Main menu)
# ---------------------------------------------------------------------------
def main_menu_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("🛒 Buy Number"),
        types.KeyboardButton("🎁 Referral"),
    )
    kb.add(
        types.KeyboardButton("👤 Profile"),
        types.KeyboardButton("💳 Deposit"),
    )
    kb.add(
        types.KeyboardButton("🆘 Support"),
        types.KeyboardButton("⚙️ Method"),
    )
    if is_admin(user_id):
        kb.add(types.KeyboardButton("👮 Admin Panel"))
    return kb


def back_to_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("⬅️ Back to Menu"))
    return kb


def back_only_keyboard():
    """শুধু Back বাটনসহ কীবোর্ড — Bulk Buy quantity ইনপুট ধাপে ব্যবহার হয়,
    এখান থেকে Back করলে buy মেনুতে (main menu তে নয়) ফিরে যায়।"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("⬅️ Back"))
    return kb


# ---------------------------------------------------------------------------
# KEYBOARDS (Inline)
# ---------------------------------------------------------------------------
def buy_number_type_inline():
    """দুইটা ইনলাইন বাটন একটার নিচে আরেকটা (vertically stacked) থাকবে।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="prod_whatsapp"))
    kb.add(types.InlineKeyboardButton("✈️ Telegram Number", callback_data="prod_telegram"))
    return kb


def product_detail_keyboard():
    """3 keyboard buttons shown after selecting product, stacked vertically:
    Buy one pcs / Bulk Buy / Back"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(types.KeyboardButton("🛍️ Buy one pcs"))
    kb.add(types.KeyboardButton("📦 Bulk Buy"))
    kb.add(types.KeyboardButton("⬅️ Back"))
    return kb


def referral_inline():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔗 Share your link and earn!", switch_inline_query="join_now"))
    return kb


def upload_file_product_inline():
    """Admin ফাইল আপলোডের আগে কোন প্রোডাক্টের স্টক আপডেট হবে সেটা বেছে নেয়।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="uploadprod_whatsapp"),
        types.InlineKeyboardButton("✈️ Telegram Number", callback_data="uploadprod_telegram"),
    )
    return kb


def upload_confirm_inline():
    """Stock ফাইল parse হওয়ার পর আসলে stock এ যুক্ত করার আগে কনফার্মেশন।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Confirm & Add", callback_data="stockup_confirm"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="stockup_cancel"),
    )
    return kb


def stock_broadcast_confirm_inline():
    """নতুন Stock Add হওয়ার পর সেই আপডেটটা সব ইউজারকে broadcast করবে কিনা জিজ্ঞেস করে।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ হ্যাঁ, Broadcast করুন", callback_data="stockbroadcast_yes"),
        types.InlineKeyboardButton("❌ না", callback_data="stockbroadcast_no"),
    )
    return kb


def deposit_balance_inline():
    """Deposit ফ্লো শুরু হওয়ার সময় Balance স্ক্রিনে দেখানো ➕ Deposit বাটন।"""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Deposit", callback_data="dep_start"))
    return kb


def deposit_methods_inline():
    """ইউজারকে Deposit মেথড বেছে নেওয়ার বাটন দেখায় (bot_settings['deposit_methods'] থেকে),
    ২টা করে এক সারিতে গ্রিড আকারে।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(f"💳 {m}", callback_data=f"depmethod_{m}")
        for m in bot_settings["deposit_methods"]
    ]
    for i in range(0, len(buttons), 2):
        kb.row(*buttons[i:i + 2])
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="dep_back"))
    return kb


def deposit_cancel_inline():
    """Deposit amount ইনপুট ধাপে Back এর বদলে দেখানো Cancel বাটন।"""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="deposit_cancel"))
    return kb


def deposit_review_inline(dep_id):
    """Admin কে পাঠানো pending deposit নোটিফিকেশনে Approve/Reject বাটন।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"depapprove_{dep_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"depreject_{dep_id}"),
    )
    return kb


def _track_deposit_admin_msg(dep, chat_id, message_id):
    """যতগুলো Admin-কে (বা যতবার Pending Deposits লিস্টে) এই deposit-এর
    Approve/Reject বাটনসহ মেসেজ পাঠানো হয়েছে, তার chat_id/message_id মনে রাখে।
    যাতে একজন Admin Approve/Reject করার পর বাকি সব কপি থেকেও বাটন সরিয়ে দেওয়া যায়
    (অন্য কোনো Admin যেন পুরনো বাটনে ক্লিক করে বিভ্রান্ত না হয়)।"""
    dep.setdefault("admin_msgs", []).append({"chat_id": chat_id, "message_id": message_id})


def _clear_deposit_admin_buttons(dep):
    """dep['admin_msgs'] এ জমা থাকা সব মেসেজ থেকে Approve/Reject বাটন সরিয়ে দেয়
    (best-effort — কোনো মেসেজ এডিট করতে না পারলে চুপচাপ স্কিপ করে)।"""
    for ref in dep.get("admin_msgs", []):
        try:
            bot.edit_message_reply_markup(ref["chat_id"], ref["message_id"], reply_markup=None)
        except Exception:
            pass


def deposit_numbers_inline():
    """Admin প্যানেলে ম্যানুয়াল মেথডগুলোর পেমেন্ট নাম্বার/অ্যাড্রেস সেট করার বাটন।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for m in DEPOSIT_MANUAL_METHODS:
        num = bot_settings["deposit_numbers"].get(m) or "❌ সেট করা নেই"
        kb.add(types.InlineKeyboardButton(f"{m}: {num}", callback_data=f"depnum_{m}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="settings_back"))
    return kb


def set_price_product_inline():
    """Admin price পরিবর্তনের আগে কোন প্রোডাক্টের price বদলাবে সেটা বেছে নেয়।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="priceprod_whatsapp"),
        types.InlineKeyboardButton("✈️ Telegram Number", callback_data="priceprod_telegram"),
    )
    return kb


def admin_panel_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Upload File", callback_data="admin_upload_file"),
        types.InlineKeyboardButton("💰 Set Price", callback_data="admin_set_price_stock"),
    )
    kb.add(
        types.InlineKeyboardButton("💱 Set Dollar Rate", callback_data="admin_set_usd_rate"),
    )
    kb.add(
        types.InlineKeyboardButton("👥 Users List", callback_data="admin_users_list"),
        types.InlineKeyboardButton("📊 Statistics", callback_data="admin_statistics"),
    )
    kb.add(
        types.InlineKeyboardButton("🔎 User Info", callback_data="admin_user_info"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("💵 Add/Remove Balance", callback_data="admin_balance_edit"),
    )
    kb.add(
        types.InlineKeyboardButton("🧾 Orders", callback_data="admin_orders"),
        types.InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin_bot_settings"),
    )
    kb.add(
        types.InlineKeyboardButton("💰 Deposit Requests", callback_data="admin_deposit_requests"),
        types.InlineKeyboardButton("📮 Deposit Numbers", callback_data="admin_deposit_numbers"),
    )
    kb.add(
        types.InlineKeyboardButton("📤 Export DB", callback_data="admin_export_db"),
        types.InlineKeyboardButton("📥 Import DB", callback_data="admin_import_db"),
    )
    kb.add(types.InlineKeyboardButton("📦 Unsold Product Export", callback_data="admin_export_unsold"))
    kb.add(types.InlineKeyboardButton("🔐 Force Join", callback_data="admin_force_join"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_back_to_menu"))
    return kb


def bot_settings_inline():
    """⚙️ Bot Settings সাব-মেনু: বর্তমান ভ্যালুসহ বাটন দেখায়, চাপলে বদলানো যায়।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            f"🎁 Referral Bonus: {bot_settings['referral_bonus']} BDT",
            callback_data="settings_referral_bonus",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            f"💳 Deposit Limit: {bot_settings['min_deposit']} - {bot_settings['max_deposit']} BDT",
            callback_data="settings_deposit_limits",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "💳 Deposit Methods: " + ", ".join(bot_settings["deposit_methods"]),
            callback_data="settings_deposit_methods",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🛠️ Maintenance Mode: " + ("✅ ON" if bot_settings["maintenance_mode"] else "❌ OFF"),
            callback_data="settings_toggle_maintenance",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🆘 Support Username: " + (bot_settings["support_username"] or "সেট করা নেই"),
            callback_data="settings_support_username",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            f"🎥 Method Videos: {len(bot_settings['method_videos'])} টি যুক্ত আছে",
            callback_data="settings_method_video",
        )
    )
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="settings_back"))
    return kb


# ---------------------------------------------------------------------------
# TEXT TEMPLATES
# ---------------------------------------------------------------------------
def profile_text(u):
    """প্রোফাইলের ডিটেইলস Bold লেবেল ফরম্যাটে; শুধু User ID <code> (monospace) এ
    থাকে (কপি করার সুবিধার জন্য), বাকি সব ভ্যালু সাধারণ (regular) টেক্সটে থাকে।"""
    return (
        f"👤 <b>Profile</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{u['id']}</code>\n"
        f"👤 <b>Full Name:</b> {u['full_name']}\n"
        f"📝 <b>Username:</b> {u['username']}\n"
        f"💰 <b>Balance:</b> {fmt_amount(u['balance'])}\n"
        f"📊 <b>Total Purchased:</b> {u['total_purchased']}\n"
        f"💸 <b>Today Spent:</b> {fmt_amount(u['today_spent'])}\n"
        f"💳 <b>Today Deposit:</b> {fmt_amount(u['today_deposit'])}"
    )


def referral_text(user_id, data):
    return (
        "🎁 <b>Referral Program</b>\n\n\n"
        f"🎁 Your Referral Link:\nhttps://t.me/{BOT_USERNAME}?start={user_id}\n\n"
        f"💰 Bonus per referral: {fmt_amount(bot_settings['referral_bonus'])}\n"
        f"👥 Total referrals: {data['referrals']}\n"
        f"💵 Total earned: {fmt_amount(data['earned'])}"
    )


def product_detail_text(p):
    return (
        f"<b>{p['name']}</b>\n\n"
        f"💵 Price: {fmt_amount(p['price'])}\n"
        f"📦 Total Stock: {p['stock']}\n"
        f"📝 Description: {p['description']}"
    )


def otp_link_display(otp_link):
    """আসল URL হলে ক্লিকযোগ্য লিংক হিসেবে, না হলে (যেমন 'N/A' বা placeholder) mono টেক্সট হিসেবে দেখায়।"""
    if otp_link and str(otp_link).startswith(("http://", "https://")):
        return f'<a href="{otp_link}">🔗 OTP Link</a>'
    return f"<code>{otp_link}</code>"


def purchase_success_text(order):
    return (
        "🎉 <b>Purchase Successful!</b>\n\n"
        f"🆔 <b>Order ID:</b> <code>{order['order_id']}</code>\n"
        f"📦 <b>Product:</b> <code>{order['product_name']}</code>\n"
        f"🔢 <b>Quantity:</b> <code>{order['qty']} pcs</code>\n"
        f"💵 <b>Per piece:</b> <code>{fmt_amount(order['price'])}</code>\n"
        f"💰 <b>Total:</b> <code>{fmt_amount(order['total'])}</code>\n"
        f"💳 <b>Remaining Balance:</b> <code>{fmt_amount(order['remaining_balance'])}</code>\n"
        f"📅 <b>Date:</b> <code>{order['date']}</code>\n\n"
        f"👨‍💻 <b>Number:</b> <code>{order.get('number', 'N/A')}</code>\n"
        f"📥 <b>OTP Link:</b> {otp_link_display(order.get('otp_link', 'N/A'))}"
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if maintenance_block(message):
        return

    uid = message.from_user.id
    is_new_user = uid not in users

    u = get_user(message)
    u["id"] = uid
    user_state[uid] = {"menu": "main"}
    if is_new_user:
        save_db()   # ✅ নতুন ইউজার তৈরি হলো, ডিস্কে persist করা হলো (persistent DB)

    # --- Referral: /start <referrer_id> ---
    parts = (message.text or "").split(maxsplit=1)
    if is_new_user and len(parts) > 1 and parts[1].strip().isdigit():
        referrer_id = int(parts[1].strip())
        if referrer_id != uid and referrer_id in users and not u.get("referred_by"):
            u["referred_by"] = referrer_id
            referrer = users[referrer_id]
            bonus = bot_settings["referral_bonus"]
            referrer["referrals"] += 1
            referrer["earned"] += bonus
            referrer["balance"] += bonus
            save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
            try:
                bot.send_message(
                    referrer_id,
                    "🎉 <b>New Referral!</b>\n\n"
                    f"আপনার লিংক দিয়ে একজন নতুন ইউজার জয়েন করেছে।\n"
                    f"💰 বোনাস যুক্ত হয়েছে: {fmt_amount(bonus)}",
                )
            except Exception:
                pass  # referrer হয়তো বটকে ব্লক করেছে

    # --- Force Join: চ্যানেল/গ্রুপ সেট করা থাকলে জয়েন না করা পর্যন্ত বট ব্যবহার করতে দেওয়া হবে না ---
    if not is_admin(uid) and get_force_join_channels() and not check_user_joined_all(uid):
        u["force_join_passed"] = False
        user_state[uid] = {"menu": "force_join_wait"}
        send_force_join_prompt(message.chat.id)
        return

    u["force_join_passed"] = True
    user_state[uid] = {"menu": "main"}
    bot.send_message(message.chat.id, profile_text(u), reply_markup=main_menu_keyboard(u["id"]))


@bot.callback_query_handler(func=lambda c: c.data == "check_join")
def cb_check_join(call):
    """✅ Join Check বাটন — জয়েন হয়ে থাকলে বট আনলক করে দেয়, নাহলে আবার জয়েন করতে বলে।"""
    uid = call.from_user.id
    chat_id = call.message.chat.id
    u = get_user(call)
    u["id"] = uid

    if is_admin(uid) or check_user_joined_all(uid):
        bot.answer_callback_query(call.id, "✅ ধন্যবাদ! আপনি সব চ্যানেলে জয়েন করেছেন।")
        u["force_join_passed"] = True
        save_db()
        user_state[uid] = {"menu": "main"}
        bot.send_message(chat_id, profile_text(u), reply_markup=main_menu_keyboard(u["id"]))
    else:
        bot.answer_callback_query(
            call.id,
            "❌ আপনি এখনও সব চ্যানেল/গ্রুপে জয়েন করেননি। আগে জয়েন করে আবার চেষ্টা করুন।",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# MAIN MENU (Reply keyboard) HANDLERS
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🛒 Buy Number")
def menu_buy_number(message):
    if maintenance_block(message):
        return
    if require_force_join(message):
        return
    user_state[message.from_user.id] = {"menu": "buy_number"}
    bot.send_message(message.chat.id, "Select number type:", reply_markup=buy_number_type_inline())


@bot.message_handler(func=lambda m: m.text == "👤 Profile")
def menu_profile(message):
    if maintenance_block(message):
        return
    if require_force_join(message):
        return
    u = get_user(message)
    u["id"] = message.from_user.id
    user_state[message.from_user.id] = {"menu": "profile"}
    bot.send_message(message.chat.id, profile_text(u), reply_markup=main_menu_keyboard(u["id"]))


def deposit_balance_text(u):
    """Deposit ফ্লোর প্রথম ধাপ — বর্তমান ব্যালেন্স দেখানোর মেসেজ।"""
    return (
        "💳 <b>Deposit</b>\n\n"
        f"💰 <b>বর্তমান ব্যালেন্স:</b> <code>{fmt_amount(u['balance'])}</code>\n\n"
        "নিচের বাটনে ক্লিক করে ডিপোজিট শুরু করুন:"
    )


@bot.message_handler(func=lambda m: m.text == "💳 Deposit")
def menu_deposit(message):
    """Deposit ফ্লোর শুরু: Balance স্ক্রিন পাঠায় এবং তার message_id সেভ করে রাখে,
    যাতে পরের প্রতিটা ধাপ এই একটামাত্র মেসেজই এডিট করে দেখাতে পারে।"""
    if maintenance_block(message):
        return
    if require_force_join(message):
        return
    uid = message.from_user.id
    u = get_user(message)
    u["id"] = uid

    sent = bot.send_message(
        message.chat.id,
        deposit_balance_text(u),
        reply_markup=deposit_balance_inline(),
    )
    user_state[uid] = {"menu": "deposit_balance", "msg_id": sent.message_id}


@bot.callback_query_handler(func=lambda c: c.data == "dep_start")
def cb_deposit_start(call):
    """➕ Deposit বাটনে ক্লিক -> একই মেসেজ এডিট করে মেথড সিলেকশন গ্রিড দেখায়।"""
    if maintenance_block(call):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    chat_id = call.message.chat.id

    limit_line = ""
    if bot_settings["min_deposit"] or bot_settings["max_deposit"]:
        limit_line = (
            f"📉 Min Deposit: {fmt_amount(bot_settings['min_deposit'])}\n"
            f"📈 Max Deposit: {fmt_amount(bot_settings['max_deposit'])}\n\n"
        )

    msg_id = safe_edit_or_send(
        chat_id,
        call.message.message_id,
        "💳 <b>Deposit</b>\n\n" + limit_line + "নিচ থেকে পেমেন্ট মেথড বেছে নিন:",
        reply_markup=deposit_methods_inline(),
    )
    user_state[uid] = {"menu": "deposit_method", "msg_id": msg_id}


@bot.callback_query_handler(func=lambda c: c.data.startswith("depmethod_") or c.data == "dep_back")
def cb_deposit_method_select(call):
    if maintenance_block(call):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    chat_id = call.message.chat.id
    state = user_state.get(uid, {})
    msg_id = state.get("msg_id") or call.message.message_id

    if call.data == "dep_back":
        u = get_user(call)
        u["id"] = uid
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            deposit_balance_text(u),
            reply_markup=deposit_balance_inline(),
        )
        user_state[uid] = {"menu": "deposit_balance", "msg_id": msg_id}
        return

    method = call.data.replace("depmethod_", "")
    if method not in bot_settings["deposit_methods"]:
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            "⚠️ এই মেথডটি আর available নেই। আবার Deposit মেনু থেকে চেষ্টা করুন।",
            reply_markup=deposit_methods_inline(),
        )
        user_state[uid] = {"menu": "deposit_method", "msg_id": msg_id}
        return

    num = bot_settings["deposit_numbers"].get(method)
    num_line = f"📮 এই নাম্বার/অ্যাড্রেসে টাকা পাঠান: <code>{num}</code>\n\n" if num else ""

    if method == "Binance":
        rate = bot_settings.get("usd_rate") or 0
        rate_line = f"💱 বর্তমান রেট: 1 USDT = {rate} BDT\n\n" if rate else ""
        prompt = (
            f"💳 <b>{method} Deposit</b>\n\n{num_line}{rate_line}"
            "Deposit করার amount USDT তে লিখে পাঠান (শুধু সংখ্যা):"
        )
    else:
        prompt = f"💳 <b>{method} Deposit</b>\n\n{num_line}Deposit করার amount লিখে পাঠান (শুধু সংখ্যা):"

    msg_id = safe_edit_or_send(chat_id, msg_id, prompt, reply_markup=deposit_cancel_inline())
    user_state[uid] = {"menu": "deposit_amount", "method": method, "msg_id": msg_id}
    bot.register_next_step_handler(call.message, process_deposit_amount)


@bot.callback_query_handler(func=lambda c: c.data == "deposit_cancel")
def cb_deposit_cancel(call):
    """Deposit amount ইনপুট ধাপে Cancel বাটনে ক্লিক করলে pending input বাতিল করে
    একই মেসেজটা এডিট করে Cancel মেসেজ দেখায় (নতুন মেসেজ যায় না)।"""
    uid = call.from_user.id
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(chat_id)
    state = user_state.get(uid, {})
    msg_id = state.get("msg_id") or call.message.message_id

    safe_edit_or_send(
        chat_id,
        msg_id,
        "❌ <b>Deposit Cancelled!</b>\n\nDeposit প্রক্রিয়াটি বাতিল করা হয়েছে।",
        reply_markup=None,
    )
    user_state[uid] = {"menu": "main"}


def process_deposit_amount(message):
    """ইউজারের দেওয়া amount validate করে min/max লিমিট চেক করে, তারপর TrxID/Binance Username চায়।
    Binance এর জন্য ইউজার USDT এ amount দেয়; bot_settings['usd_rate'] দিয়ে BDT এ কনভার্ট করে।
    ইউজারের টাইপ করা মেসেজ (এই message অবজেক্ট) কখনো ডিলিট/এডিট করা হয় না — শুধু আগের
    অ্যাংকর বট-মেসেজটাই (state এর msg_id) এডিট হয়।"""
    uid = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    method = state.get("method")
    msg_id = state.get("msg_id")
    if state.get("menu") != "deposit_amount" or not method:
        bot.send_message(chat_id, "⚠️ আগে 💳 Deposit মেনু থেকে একটা মেথড বেছে নিন।")
        return

    try:
        entered = float(text)
    except ValueError:
        entered = None

    unit = "USDT" if method == "Binance" else "সংখ্যায়"
    if entered is None or entered <= 0:
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            f"⚠️ সঠিক amount {unit} লিখুন (যেমন: 100)।",
            reply_markup=deposit_cancel_inline(),
        )
        state["msg_id"] = msg_id
        bot.register_next_step_handler(message, process_deposit_amount)
        return

    amount_usdt = None
    rate = None
    if method == "Binance":
        rate = bot_settings.get("usd_rate") or 0
        if rate <= 0:
            safe_edit_or_send(
                chat_id,
                msg_id,
                "⚠️ এখনো USDT rate সেট করা হয়নি। Admin কে জানান, তারপর আবার চেষ্টা করুন।",
                reply_markup=None,
            )
            user_state[uid] = {"menu": "main"}
            return
        amount_usdt = entered
        if amount_usdt == int(amount_usdt):
            amount_usdt = int(amount_usdt)
        amount = round(entered * rate, 2)
    else:
        amount = entered

    min_dep = bot_settings["min_deposit"]
    max_dep = bot_settings["max_deposit"]
    if min_dep and amount < min_dep:
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            f"⚠️ Minimum deposit amount {fmt_amount(min_dep)}। আবার লিখুন।",
            reply_markup=deposit_cancel_inline(),
        )
        state["msg_id"] = msg_id
        bot.register_next_step_handler(message, process_deposit_amount)
        return
    if max_dep and amount > max_dep:
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            f"⚠️ Maximum deposit amount {fmt_amount(max_dep)}। আবার লিখুন।",
            reply_markup=deposit_cancel_inline(),
        )
        state["msg_id"] = msg_id
        bot.register_next_step_handler(message, process_deposit_amount)
        return

    if amount == int(amount):
        amount = int(amount)

    if method == "Binance":
        prompt = (
            f"💳 Binance — Amount: {amount_usdt} USDT (1 USDT = {rate} BDT) = {fmt_amount(amount)}\n\n"
            "send your binance username:👇"
        )
    else:
        prompt = f"💳 {method} — Amount: {fmt_amount(amount)}\n\nsend your transaction id(TrxID):👇"

    msg_id = safe_edit_or_send(chat_id, msg_id, prompt, reply_markup=deposit_cancel_inline())

    new_state = {"menu": "deposit_trxid", "method": method, "amount": amount, "msg_id": msg_id}
    if amount_usdt is not None:
        new_state["amount_usdt"] = amount_usdt
    user_state[uid] = new_state
    bot.register_next_step_handler(message, process_deposit_trxid)


def process_deposit_trxid(message):
    """TrxID/Username নিয়ে deposit রিকোয়েস্ট তৈরি করে — ম্যানুয়াল মেথড হলে Admin রিভিউতে
    পাঠায়, নাহলে সাথে সাথে ব্যালেন্স যোগ করে অটো-অ্যাপ্রুভ করে দেয়। প্রতিটা ধাপ একই
    অ্যাংকর মেসেজ (state এর msg_id) এডিট করে দেখায়; ইউজারের টাইপ করা মেসেজ অক্ষত থাকে।"""
    uid = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    method = state.get("method")
    amount = state.get("amount")
    msg_id = state.get("msg_id")
    if state.get("menu") != "deposit_trxid" or not method or amount is None:
        bot.send_message(chat_id, "⚠️ আগে 💳 Deposit মেনু থেকে আবার শুরু করুন।")
        return

    trx_id = text
    if not trx_id:
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            f"⚠️ সঠিক {_id_label(method)} লিখুন।",
            reply_markup=deposit_cancel_inline(),
        )
        state["msg_id"] = msg_id
        bot.register_next_step_handler(message, process_deposit_trxid)
        return

    u = get_user(message)
    u["id"] = uid

    # 🛡️ Duplicate TrxID guard: একই TrxID দিয়ে আগেই কোনো Pending/Approved Deposit
    # থাকলে নতুন করে আরেকটা Deposit request তৈরি হতে দেওয়া হবে না — নাহলে Admin
    # (বা SMS auto-approve) না বুঝে দুইটা আলাদা Request-ই Approve করে ফেললে একই
    # আসল পেমেন্টের জন্য দুইবার ব্যালেন্স যোগ হয়ে যেতে পারে।
    existing_trx = next(
        (
            d for d in deposits.values()
            if d.get("method") == method
            and (d.get("trx_id") or "").strip().upper() == trx_id.strip().upper()
            and d.get("status") in ("pending", "approved")
        ),
        None,
    )
    if existing_trx:
        status_bn = (
            "ইতিমধ্যে ✅ Approved হয়ে গেছে"
            if existing_trx["status"] == "approved"
            else "ইতিমধ্যে ⏳ Pending অবস্থায় Admin রিভিউতে আছে"
        )
        msg_id = safe_edit_or_send(
            chat_id,
            msg_id,
            f"⚠️ এই {_id_label(method)} (<code>{trx_id}</code>) দিয়ে আগেই একটা Deposit "
            f"(DEP-{existing_trx['id']}) {status_bn}। একই TrxID দিয়ে একাধিকবার Deposit "
            "request করা যায় না।\n\nভুল হয়ে থাকলে সঠিক TrxID দিয়ে আবার চেষ্টা করুন, "
            "অথবা 🆘 Support এ যোগাযোগ করুন।",
            reply_markup=deposit_cancel_inline(),
        )
        state["msg_id"] = msg_id
        bot.register_next_step_handler(message, process_deposit_trxid)
        return

    dep_id = _next_deposit_id()
    dep = {
        "id": dep_id,
        "user_id": uid,
        "method": method,
        "amount": amount,
        "trx_id": trx_id,
        "status": "pending",
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if state.get("amount_usdt") is not None:
        dep["amount_usdt"] = state["amount_usdt"]
    deposits[dep_id] = dep
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    # -----------------------------------------------------------------
    # bKash / Nagad / Rocket -> প্রথমে চেক হয় SMS Forwarder থেকে আগে থেকেই
    # কোনো ম্যাচিং SMS এসে গেছে কিনা (ইউজার SMS আসার পরে TrxID লিখলে) —
    # মিললে সাথে সাথে অটো-অ্যাপ্রুভ, ভুল তথ্য দিলে সাথে সাথে reject। কোনো
    # ম্যাচ না পেলে (SMS এখনো আসেনি) Admin Approve/Reject এর জন্য pending থাকবে।
    # Binance -> কোনো SMS আসে না, তাই auto-match চেষ্টাই করা হয় না — সবসময়
    # সরাসরি pending থেকে Admin ম্যানুয়ালি Approve/Reject করবে।
    # বাকি সব (অন্য যেকোনো) মেথড -> সাথে সাথে অটো-অ্যাপ্রুভ, SMS লাগে না।
    # -----------------------------------------------------------------
    if method in DEPOSIT_MANUAL_METHODS:
        if method in SMS_AUTO_APPROVE_METHODS:
            sms_match = try_auto_approve_from_stored_sms(dep)
            if sms_match in ("approved", "rejected_mismatch"):
                # ইউজার/এডমিন নোটিফিকেশন try_auto_approve_from_stored_sms() থেকেই পাঠানো হয়ে গেছে।
                user_state[uid] = {"menu": "main"}
                return

        usdt_line = f"💵 USDT Amount: {dep['amount_usdt']} USDT\n" if dep.get("amount_usdt") is not None else ""
        bot.send_message(
            chat_id,
            "⏳ <b>Deposit request submitted!</b>\n\n"
            f"🆔 Request: DEP-{dep_id}\n"
            f"💳 Method: {method}\n"
            f"{usdt_line}"
            f"💰 Amount: {fmt_amount(amount)}\n"
            f"🧾 {_id_label(method)}: {trx_id}\n\n"
            "Payment verify হলে আটোমেটিক approve হবে",
        )
        user_state[uid] = {"menu": "main"}
        admin_text = (
            "💰 <b>New Deposit Request</b>\n\n"
            f"🆔 Request: DEP-{dep_id}\n"
            f"👤 User: {u['full_name']} ({u['username']}) | <code>{uid}</code>\n"
            f"💳 Method: {method}\n"
            f"{usdt_line}"
            f"💰 Amount: {fmt_amount(amount)}\n"
            f"🧾 {_id_label(method)}: <code>{trx_id}</code>"
        )
        for admin_id in ADMIN_IDS:
            try:
                sent = bot.send_message(admin_id, admin_text, reply_markup=deposit_review_inline(dep_id))
                _track_deposit_admin_msg(dep, admin_id, sent.message_id)
            except Exception:
                pass
        save_db()   # ✅ admin_msgs রেফারেন্স ডিস্কে persist করা হলো
    else:
        dep["status"] = "approved"
        u["balance"] += amount
        u["today_deposit"] += amount
        save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
        bot.send_message(
            chat_id,
            "✅ <b>Deposit Auto-Approved!</b>\n\n"
            f"🆔 Request: DEP-{dep_id}\n"
            f"💳 Method: {method}\n"
            f"💰 +{fmt_amount(amount)} added\n"
            f"💰 New Balance: {fmt_amount(u['balance'])}",
        )
        user_state[uid] = {"menu": "main"}
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    "🤖 <b>Auto-Approved Deposit</b>\n\n"
                    f"🆔 DEP-{dep_id} | 👤 <code>{uid}</code> | 💳 {method} | "
                    f"💰 {fmt_amount(amount)} | 🧾 {trx_id}",
                )
            except Exception:
                pass


@bot.callback_query_handler(
    func=lambda c: (c.data.startswith("depapprove_") or c.data.startswith("depreject_"))
    and is_admin(c.from_user.id)
)
def cb_deposit_review(call):
    """Admin এর Approve/Reject বাটনে ক্লিক হ্যান্ডল করে।

    🔒 Race-condition fix: দুইজন Admin (বা একজন Admin ডাবল-ট্যাপ করলে) প্রায়
    একই মুহূর্তে Approve/Reject চাপলে যেন দুইবার ব্যালেন্স যোগ না হয়ে যায়,
    তাই "status এখনও pending কিনা চেক করা -> approved/rejected এ সেট করা ->
    ব্যালেন্স যোগ করা" — পুরো অংশটা _deposit_lock দিয়ে atomic রাখা হয়েছে।

    🛡️ Data-safety fix: ইউজার রেকর্ড কোনো কারণে খুঁজে না পেলে (যেমন ডেটা
    করাপশন) deposit-টা approved মার্ক করে ব্যালেন্স যোগ না করে ফেলে রাখার
    বদলে সেটা pending-ই রেখে Admin-কে সরাসরি এরর জানানো হয়, যাতে
    "Approved হয়ে গেছে কিন্তু ব্যালেন্স যোগ হয়নি" — এই অবস্থা কখনো তৈরি না হয়।
    """
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id

    if call.data.startswith("depapprove_"):
        action = "approve"
        dep_id = int(call.data.replace("depapprove_", ""))
    else:
        action = "reject"
        dep_id = int(call.data.replace("depreject_", ""))

    dep = deposits.get(dep_id)
    if not dep:
        bot.send_message(chat_id, "⚠️ এই Deposit Request খুঁজে পাওয়া যায়নি।")
        return

    with _deposit_lock:
        if dep["status"] != "pending":
            bot.send_message(
                chat_id,
                f"⚠️ এই Deposit ইতিমধ্যে '{dep['status']}' করা হয়ে গেছে (অন্য কোনো Admin আগেই "
                "রিভিউ করে ফেলেছেন) — আবার কিছু করা হয়নি।",
            )
            _clear_deposit_admin_buttons(dep)
            return

        user_id = dep["user_id"]
        u = users.get(user_id)

        if action == "approve":
            if u is None:
                # ইউজার রেকর্ড খুঁজে পাওয়া যায়নি — ব্যালেন্স ছাড়া Approve করে দিলে
                # সেটা "Approved কিন্তু balance যোগ হয়নি" বাগে পরিণত হবে, তাই
                # pending-ই রেখে Admin-কে জানানো হচ্ছে।
                bot.send_message(
                    chat_id,
                    f"❌ DEP-{dep_id}: ইউজার (<code>{user_id}</code>) খুঁজে পাওয়া যায়নি, তাই "
                    "Approve করা যায়নি (ব্যালেন্স যোগ হয়নি)। এটা এখনও 'pending' অবস্থায় আছে — "
                    "Export DB দিয়ে ডেটা চেক করুন।",
                )
                return

            dep["status"] = "approved"
            u["balance"] += dep["amount"]
            u["today_deposit"] += dep["amount"]
            save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
            _clear_deposit_admin_buttons(dep)   # অন্য সব Admin-এর কপি থেকেও বাটন সরানো হলো

            bot.send_message(chat_id, f"✅ DEP-{dep_id} Approved হয়েছে।")
            try:
                bot.send_message(
                    user_id,
                    "✅ <b>Deposit Approved!</b>\n\n"
                    f"🆔 Request: DEP-{dep_id}\n"
                    f"💰 +{fmt_amount(dep['amount'])} added\n"
                    f"💰 New Balance: {fmt_amount(u['balance'])}",
                )
            except Exception:
                pass
        else:
            dep["status"] = "rejected"
            save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
            _clear_deposit_admin_buttons(dep)   # অন্য সব Admin-এর কপি থেকেও বাটন সরানো হলো

            bot.send_message(chat_id, f"❌ DEP-{dep_id} Rejected হয়েছে।")
            try:
                bot.send_message(
                    user_id,
                    f"❌ <b>Deposit Rejected.</b>\n\n🆔 Request: DEP-{dep_id}\nProblem হলে Support এ যোগাযোগ করুন।",
                )
            except Exception:
                pass


@bot.message_handler(func=lambda m: m.text == "🎁 Referral")
def menu_referral(message):
    if maintenance_block(message):
        return
    if require_force_join(message):
        return
    u = get_user(message)
    u["id"] = message.from_user.id
    user_state[message.from_user.id] = {"menu": "referral"}
    bot.send_message(
        message.chat.id,
        referral_text(u["id"], u),
        reply_markup=referral_inline(),
    )


DEVELOPER_USERNAME = "relax1472"   # hard-coded, Bot Settings দিয়ে বদলানো যায় না


@bot.message_handler(func=lambda m: m.text == "🆘 Support")
def menu_support(message):
    if maintenance_block(message):
        return
    if require_force_join(message):
        return
    # main menu keyboard এই থেকেই যায় — সাপোর্ট আলাদা কোনো sub-menu এ যায় না
    user_state[message.from_user.id] = {"menu": "main"}

    kb = types.InlineKeyboardMarkup()
    support_username = (bot_settings.get("support_username") or "").lstrip("@").strip()
    if support_username:
        kb.add(types.InlineKeyboardButton("✅ 24/7 live chat", url=f"https://t.me/{support_username}"))

    bot.send_message(
        message.chat.id,
        "☎️ <b>Support</b>\n\n"
        f"👨‍💻 Developer: @{DEVELOPER_USERNAME}\n\n"
        "📞 Need help? Contact us: 👇",
        reply_markup=kb,
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Method")
def menu_method(message):
    if maintenance_block(message):
        return
    if require_force_join(message):
        return
    user_state[message.from_user.id] = {"menu": "main"}

    videos = bot_settings.get("method_videos") or []
    if not videos:
        bot.send_message(
            message.chat.id,
            "⚙️ <b>Method</b>\n\nএখনো কোনো টিউটোরিয়াল ভিডিও সেট করা হয়নি।",
        )
        return

    kb = types.InlineKeyboardMarkup()
    for v in videos:
        kb.add(types.InlineKeyboardButton(v["title"], url=v["link"]))
    bot.send_message(
        message.chat.id,
        "⚙️ <b>Method</b>\n\nকিভাবে ডিপোজিট/পারচেজ করবেন তা এই ভিডিওগুলোতে দেখুন: 👇",
        reply_markup=kb,
    )


@bot.message_handler(func=lambda m: m.text in ["⬅️ Back to Menu", "⬅️ Back"])
def go_back(message):
    """সব জায়গা থেকে ধাপে ধাপে Back করার লজিক।"""
    uid = message.from_user.id

    # Buy Number মেনুর যেকোনো ধাপ (product select / product detail / bulk buy ইনপুট) থেকে
    # Back চাপলে সরাসরি Main Menu তে ফিরে যেতে হবে।
    # সব জায়গা থেকে -> main menu
    u = get_user(message)
    u["id"] = uid
    user_state[uid] = {"menu": "main"}
    bot.send_message(message.chat.id, "🏠 Main Menu", reply_markup=main_menu_keyboard(uid))


# ---------------------------------------------------------------------------
# INLINE CALLBACKS: product selection (WhatsApp / Telegram)
# ---------------------------------------------------------------------------
def show_product_detail(chat_id, uid, product_key):
    """প্রোডাক্ট ডিটেইলস + buy মেনু (Buy one pcs/Bulk Buy/Back) দেখায়।
    প্রথমবার প্রোডাক্ট সিলেক্ট করার সময় এবং Bulk Buy থেকে Back করার সময় দুই জায়গাতেই ব্যবহার হয়।"""
    p = products.get(product_key)
    if not p:
        return
    user_state[uid] = {"menu": "product_detail", "product": product_key}
    bot.send_message(chat_id, product_detail_text(p))
    bot.send_message(chat_id, "Please choose an option below:", reply_markup=product_detail_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("prod_"))
def cb_product_select(call):
    if require_force_join(call):
        bot.answer_callback_query(call.id)
        return
    if maintenance_block(call):
        bot.answer_callback_query(call.id)
        return
    product_key = call.data.replace("prod_", "")   # "whatsapp" / "telegram"
    p = products.get(product_key)
    if not p:
        bot.answer_callback_query(call.id, "Product not found.")
        return

    bot.answer_callback_query(call.id)
    show_product_detail(call.message.chat.id, call.from_user.id, product_key)


# ---------------------------------------------------------------------------
# PRODUCT DETAIL MENU: Buy one pcs / Bulk Buy
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🛍️ Buy one pcs")
def buy_one_pcs(message):
    if maintenance_block(message):
        return
    uid = message.from_user.id
    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)

    if not p:
        bot.send_message(message.chat.id, "⚠️ আগে একটা প্রোডাক্ট সিলেক্ট করুন।")
        return

    u = get_user(message)
    u["id"] = uid

    # --- validation ---
    if p["price"] <= 0:
        bot.send_message(message.chat.id, "⚠️ এই প্রোডাক্টের দাম এখনও সেট করা হয়নি। এডমিনের সাথে যোগাযোগ করুন।")
        return

    # 🔒 চেক + pop + deduct + save — পুরোটা লকের ভেতরে, যাতে দুইজন ইউজার
    # একসাথে কিনলে একই item / ভুল স্টক গণনা না হয়।
    with _stock_lock:
        stock_list = p.setdefault("stock_list", [])
        if not stock_list:
            bot.send_message(
                message.chat.id,
                "❌ <b>Stock Out!</b>\n\nদুঃখিত, এই প্রোডাক্টের স্টক এখন খালি। কিছুক্ষণ পরে চেষ্টা করুন।",
                reply_markup=product_detail_keyboard(),
            )
            return

        if u["balance"] < p["price"]:
            bot.send_message(
                message.chat.id,
                "❌ <b>Insufficient Balance!</b>\n\n"
                f"💰 আপনার ব্যালেন্স: {fmt_amount(u['balance'])}\n"
                f"💵 প্রয়োজন: {fmt_amount(p['price'])}\n\n"
                "অনুগ্রহ করে আগে Deposit করুন।",
                reply_markup=product_detail_keyboard(),
            )
            return

        # --- fulfill purchase (real balance + stock deduction) ---
        item = stock_list.pop(0)
        p["stock"] = len(stock_list)

        u["balance"] -= p["price"]
        u["total_purchased"] += 1
        u["today_spent"] += p["price"]

        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        order = {
            "order_id": order_id,
            "user_id": uid,
            "product_name": p["name"],
            "qty": 1,
            "price": p["price"],
            "total": p["price"],
            "remaining_balance": u["balance"],
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "number": item["number"],
            "otp_link": item["otp_link"],
        }
        orders[order_id] = order
        save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    bot.send_message(message.chat.id, purchase_success_text(order), reply_markup=product_detail_keyboard())


@bot.message_handler(func=lambda m: m.text == "📦 Bulk Buy")
def bulk_buy(message):
    if maintenance_block(message):
        return
    uid = message.from_user.id
    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)

    if not p:
        bot.send_message(message.chat.id, "⚠️ আগে একটা প্রোডাক্ট সিলেক্ট করুন।")
        return

    if p["price"] <= 0:
        bot.send_message(message.chat.id, "⚠️ এই প্রোডাক্টের দাম এখনও সেট করা হয়নি। এডমিনের সাথে যোগাযোগ করুন।")
        return

    stock_list = p.setdefault("stock_list", [])
    if not stock_list:
        bot.send_message(
            message.chat.id,
            "❌ <b>Stock Out!</b>\n\nদুঃখিত, এই প্রোডাক্টের স্টক এখন খালি।",
            reply_markup=product_detail_keyboard(),
        )
        return

    bot.send_message(
        message.chat.id,
        "📦 <b>Bulk Buy</b>\n\n"
        f"📊 বর্তমান স্টক: {len(stock_list)} পিস\n"
        f"💵 প্রতি পিস: {fmt_amount(p['price'])}\n\n"
        "কত পিস কিনতে চান, সংখ্যায় লিখে পাঠান (যেমন: 5):",
        reply_markup=back_only_keyboard(),
    )
    bot.register_next_step_handler(message, process_bulk_quantity)


def process_bulk_quantity(message):
    """Bulk Buy এর quantity ইনপুট প্রসেস করে, balance/stock check করে অর্ডার সম্পন্ন করে।"""
    uid = message.from_user.id
    text = (message.text or "").strip()
    state = user_state.get(uid, {})

    # Bulk Buy ধাপ থেকে Back চাপলে main menu তে না গিয়ে buy মেনুতে (product detail) ফিরে যায়
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        product_key = state.get("product")
        if product_key and products.get(product_key):
            show_product_detail(message.chat.id, uid, product_key)
        else:
            go_back(message)
        return

    product_key = state.get("product")
    p = products.get(product_key)
    if not p:
        bot.send_message(message.chat.id, "⚠️ আগে একটা প্রোডাক্ট সিলেক্ট করুন।")
        return

    if not text.isdigit() or int(text) <= 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি সংখ্যা লিখুন (যেমন: 5)।")
        bot.register_next_step_handler(message, process_bulk_quantity)
        return

    qty = int(text)

    # 🔒 চেক + pop + deduct + save — পুরোটা লকের ভেতরে, যাতে দুইজন ইউজার
    # একসাথে কিনলে (বিশেষত bulk) একই item / ভুল স্টক গণনা না হয়।
    with _stock_lock:
        stock_list = p.setdefault("stock_list", [])

        if qty > len(stock_list):
            bot.send_message(
                message.chat.id,
                "❌ <b>Stock Not Enough!</b>\n\n"
                f"📦 বর্তমান স্টক: {len(stock_list)} পিস\nএর বেশি পরিমাণ এখন কেনা সম্ভব নয়।",
                reply_markup=product_detail_keyboard(),
            )
            return

        u = get_user(message)
        u["id"] = uid
        total_price = p["price"] * qty

        if u["balance"] < total_price:
            bot.send_message(
                message.chat.id,
                "❌ <b>Insufficient Balance!</b>\n\n"
                f"💰 আপনার ব্যালেন্স: {fmt_amount(u['balance'])}\n"
                f"💵 প্রয়োজন: {fmt_amount(total_price)} ({qty} x {p['price']})\n\n"
                "অনুগ্রহ করে আগে Deposit করুন।",
                reply_markup=product_detail_keyboard(),
            )
            return

        # --- fulfill purchase (real balance + stock deduction) ---
        purchased_items = [stock_list.pop(0) for _ in range(qty)]
        p["stock"] = len(stock_list)

        u["balance"] -= total_price
        u["total_purchased"] += qty
        u["today_spent"] += total_price

        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        order = {
            "order_id": order_id,
            "user_id": uid,
            "product_name": p["name"],
            "qty": qty,
            "price": p["price"],
            "total": total_price,
            "remaining_balance": u["balance"],
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "number": purchased_items[0]["number"] if qty == 1 else "📎 নিচের ফাইলে দেখুন",
            "otp_link": purchased_items[0]["otp_link"] if qty == 1 else "📎 নিচের ফাইলে দেখুন",
            "items": purchased_items,
        }
        orders[order_id] = order
        save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    if qty > 1:
        bot.send_message(message.chat.id, purchase_success_text(order))
        bot.send_message(
            message.chat.id,
            "📦 আপনি কোন ফরম্যাটে ফাইল নিতে চান?",
            reply_markup=bulk_file_format_inline(order_id),
        )
        bot.send_message(message.chat.id, "আরও কিছু কিনতে চাইলে নিচ থেকে বেছে নিন:", reply_markup=product_detail_keyboard())
    else:
        bot.send_message(message.chat.id, purchase_success_text(order), reply_markup=product_detail_keyboard())


def bulk_file_format_inline(order_id):
    """Bulk Buy এর পর ইউজার কোন ফরম্যাটে ডেটা চান (TXT / CSV / Inline) সেটা বেছে নেওয়ার বাটন।"""
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton("📄 TXT", callback_data=f"bulkfile_txt_{order_id}"),
        types.InlineKeyboardButton("📊 CSV", callback_data=f"bulkfile_csv_{order_id}"),
        types.InlineKeyboardButton("📋 Inline", callback_data=f"bulkfile_inline_{order_id}"),
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("bulkfile_"))
def cb_bulk_file_format(call):
    """Bulk Buy অর্ডারের আইটেমগুলো ইউজারের বেছে নেওয়া ফরম্যাটে (txt/csv/inline) পাঠায়।"""
    uid = call.from_user.id
    _, fmt, order_id = call.data.split("_", 2)
    order = orders.get(order_id)
    bot.answer_callback_query(call.id)

    if not order or order.get("user_id") != uid:
        bot.send_message(call.message.chat.id, "⚠️ অর্ডারটি খুঁজে পাওয়া যায়নি বা এটি আপনার অর্ডার নয়।")
        return

    items = order.get("items", [])
    if not items:
        bot.send_message(call.message.chat.id, "⚠️ এই অর্ডারে কোনো আইটেম পাওয়া যায়নি।")
        return

    product_key = next((k for k, pv in products.items() if pv["name"] == order["product_name"]), "items")

    if fmt == "inline":
        chunk_lines = [
            f"{i+1}. <code>{it['number']}</code> | {otp_link_display(it['otp_link'])}"
            for i, it in enumerate(items)
        ]
        header = f"📋 <b>আপনার {len(items)} টি {order['product_name']}</b>\n\n"
        text = header
        for line in chunk_lines:
            if len(text) + len(line) + 1 > 3500:
                bot.send_message(call.message.chat.id, text)
                text = ""
            text += line + "\n"
        if text.strip():
            bot.send_message(call.message.chat.id, text)
        return

    if fmt == "csv":
        lines = ["number,otp_link"] + [f"{it['number']},{it['otp_link']}" for it in items]
        filename = f"{order_id}_{product_key}.csv"
    else:  # txt
        lines = [f"{it['number']}|{it['otp_link']}" for it in items]
        filename = f"{order_id}_{product_key}.txt"

    file_content = "\n".join(lines).encode("utf-8")
    bot.send_document(
        call.message.chat.id,
        io.BytesIO(file_content),
        visible_file_name=filename,
        caption=f"📥 আপনার {len(items)} টি {order['product_name']}",
    )


# ---------------------------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "👮 Admin Panel" and is_admin(m.from_user.id))
def menu_admin_panel(message):
    user_state[message.from_user.id] = {"menu": "admin_panel"}
    bot.send_message(message.chat.id, "👮 <b>Admin Panel</b>", reply_markup=admin_panel_inline())


# ---------------------------------------------------------------------------
# ADMIN PANEL: inline button callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_") and is_admin(c.from_user.id))
def cb_admin_panel(call):
    uid = call.from_user.id
    data = call.data
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    if data == "admin_upload_file":
        user_state[uid] = {"menu": "admin_upload_file_select"}
        bot.send_message(
            chat_id,
            "📤 কোন প্রোডাক্টের জন্য স্টক ফাইল আপলোড করবেন?",
            reply_markup=upload_file_product_inline(),
        )

    elif data == "admin_set_price_stock":
        user_state[uid] = {"menu": "admin_set_price_select"}
        bot.send_message(
            chat_id,
            "💰 কোন প্রোডাক্টের price পরিবর্তন করবেন?",
            reply_markup=set_price_product_inline(),
        )

    elif data == "admin_set_usd_rate":
        user_state[uid] = {"menu": "admin_set_usd_rate"}
        current = bot_settings.get("usd_rate") or 0
        bot.send_message(
            chat_id,
            "💱 নতুন Dollar Rate লিখে পাঠান (1 USD = কত BDT)।\n"
            f"বর্তমান রেট: {current if current else 'সেট করা নেই'}\n\n"
            "উদাহরণ: 122 অথবা 121.50",
        )
        bot.register_next_step_handler(call.message, process_set_usd_rate)

    elif data == "admin_users_list":
        # TODO: pagination সহ real user list
        bot.send_message(chat_id, f"👥 মোট ইউজার: {len(users)}\n(তালিকা লজিক পরে যুক্ত হবে)")

    elif data == "admin_statistics":
        # TODO: real total sales, revenue, today stats ইত্যাদি
        bot.send_message(
            chat_id,
            "📊 <b>Statistics</b>\n\n"
            f"👥 Total Users: {len(users)}\n"
            f"🧾 Total Orders: {len(orders)}\n"
            "💰 Total Revenue: TODO\n"
            "📅 Today's Sales: TODO",
        )

    elif data == "admin_broadcast":
        user_state[uid] = {"menu": "admin_broadcast"}
        bot.send_message(chat_id, "📢 যে মেসেজটি সব ইউজারকে পাঠাতে চান লিখুন।")
        bot.register_next_step_handler(call.message, process_broadcast_message)

    elif data == "admin_balance_edit":
        user_state[uid] = {"menu": "admin_balance_edit_wait_uid"}
        bot.send_message(
            chat_id,
            "💵 <b>Add/Remove Balance</b>\n\n"
            "যে ইউজারের ব্যালেন্স বদলাতে চান, তার Telegram User ID লিখে পাঠান।",
            reply_markup=back_to_main_keyboard(),
        )
        bot.register_next_step_handler(call.message, process_balance_edit_uid)

    elif data == "admin_user_info":
        user_state[uid] = {"menu": "admin_user_info_wait_uid"}
        bot.send_message(
            chat_id,
            "🔎 <b>User Info</b>\n\n"
            "যে ইউজারের সব তথ্য দেখতে চান, তার Telegram User ID লিখে পাঠান।",
            reply_markup=back_to_main_keyboard(),
        )
        bot.register_next_step_handler(call.message, process_admin_user_info_uid)

    elif data == "admin_orders":
        # TODO: pagination সহ real orders list / filter
        bot.send_message(chat_id, f"🧾 মোট অর্ডার: {len(orders)}\n(তালিকা লজিক পরে যুক্ত হবে)")

    elif data == "admin_export_db":
        payload = _build_db_export_payload()
        try:
            json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Export করতে সমস্যা হয়েছে: {e}")
        else:
            filename = f"db_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            bot.send_document(
                chat_id,
                io.BytesIO(json_bytes),
                visible_file_name=filename,
                caption=(
                    "📤 <b>DB Export সম্পন্ন!</b>\n\n"
                    f"👥 Users: {len(users)}\n"
                    f"🧾 Orders: {len(orders)}\n"
                    f"💰 Deposits: {len(deposits)}\n"
                    f"📩 SMS Log: {len(sms_log)}\n\n"
                    "⚠️ এই ফাইলে ইউজারদের ব্যালেন্স/ডেটা থাকে — নিরাপদ জায়গায় রাখুন।"
                ),
            )

    elif data == "admin_export_unsold":
        try:
            wb = _build_unsold_export_workbook()
        except Exception as e:
            bot.send_message(chat_id, f"❌ Export করতে সমস্যা হয়েছে: {e}")
        else:
            if wb is None:
                bot.send_message(chat_id, "📦 এই মুহূর্তে কোনো Unsold (অবিক্রিত) প্রোডাক্ট নেই।")
            else:
                buf = io.BytesIO()
                wb.save(buf)
                buf.seek(0)
                filename = f"unsold_stock_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                total_unsold = sum(len(p.get("stock_list") or []) for p in products.values())
                bot.send_document(
                    chat_id,
                    buf,
                    visible_file_name=filename,
                    caption=(
                        "📦 <b>Unsold Product Export সম্পন্ন!</b>\n\n"
                        f"📊 মোট অবিক্রিত পিস: {total_unsold}\n"
                        "প্রতিটা প্রোডাক্টের জন্য আলাদা শীটে Number ও OTP Link দেওয়া আছে।"
                    ),
                )

    elif data == "admin_import_db":
        user_state[uid] = {"menu": "admin_import_db_wait_file"}
        bot.send_message(
            chat_id,
            "📥 <b>DB Import</b>\n\n"
            "⚠️ এটা করলে বর্তমান সব ডেটা (Users/ব্যালেন্স/Orders/Deposits/SMS Log) মুছে "
            "আপলোড করা ব্যাকআপ (.json) ফাইল দিয়ে রিপ্লেস হয়ে যাবে।\n\n"
            "আগে এই বটের Export করা .json ব্যাকআপ ফাইলটা পাঠান।",
            reply_markup=back_to_main_keyboard(),
        )

    elif data == "admin_deposit_requests":
        pending = [d for d in deposits.values() if d["status"] == "pending"]
        if not pending:
            bot.send_message(chat_id, "💰 <b>Pending Deposits</b>\n\nএখন কোনো pending deposit নেই।")
        else:
            for d in pending[:20]:
                du = users.get(d["user_id"], {})
                usdt_line = f"💵 USDT Amount: {d['amount_usdt']} USDT\n" if d.get("amount_usdt") is not None else ""
                sent = bot.send_message(
                    chat_id,
                    "💰 <b>Pending Deposit</b>\n\n"
                    f"🆔 DEP-{d['id']}\n"
                    f"👤 User: {du.get('full_name', '—')} | <code>{d['user_id']}</code>\n"
                    f"💳 Method: {d['method']}\n"
                    f"{usdt_line}"
                    f"💰 Amount: {fmt_amount(d['amount'])}\n"
                    f"🧾 {_id_label(d['method'])}: <code>{d['trx_id']}</code>\n"
                    f"📅 {d['date']}",
                    reply_markup=deposit_review_inline(d["id"]),
                )
                _track_deposit_admin_msg(d, chat_id, sent.message_id)
            save_db()   # ✅ admin_msgs রেফারেন্স ডিস্কে persist করা হলো

    elif data == "admin_deposit_numbers":
        user_state[uid] = {"menu": "admin_deposit_numbers"}
        bot.send_message(
            chat_id,
            "📮 <b>Deposit Payment Numbers</b>\n\nযে মেথডের নাম্বার/অ্যাড্রেস সেট করবেন সেটায় ক্লিক করুন।",
            reply_markup=deposit_numbers_inline(),
        )

    elif data == "admin_bot_settings":
        user_state[uid] = {"menu": "admin_bot_settings"}
        bot.send_message(
            chat_id,
            "⚙️ <b>Bot Settings</b>\n\nযে সেটিংসটি বদলাতে চান, তাতে ক্লিক করুন।",
            reply_markup=bot_settings_inline(),
        )

    elif data == "admin_force_join":
        user_state[uid] = {"menu": "admin_force_join"}
        bot.send_message(
            chat_id,
            "🔐 <b>Force Join Settings</b>\n\n"
            "ইউজার প্রথমবার বট স্টার্ট করলে নিচের চ্যানেল/গ্রুপ(গুলো)-এ জয়েন করতে "
            "বলা হবে। জয়েন না করা পর্যন্ত বট ব্যবহার করতে পারবে না।",
            reply_markup=force_join_admin_inline(),
        )

    elif data == "admin_back_to_menu":
        user_state[uid] = {"menu": "main"}
        bot.send_message(chat_id, "🏠 Main Menu", reply_markup=main_menu_keyboard(uid))


# ---------------------------------------------------------------------------
# ADMIN PANEL: 🔐 Force Join সাব-মেনুর callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("fj_") and is_admin(c.from_user.id))
def cb_force_join_admin(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data
    bot.answer_callback_query(call.id)

    if data == "fj_noop":
        return

    if data == "fj_back":
        user_state[uid] = {"menu": "admin_panel"}
        bot.send_message(chat_id, "👮 <b>Admin Panel</b>", reply_markup=admin_panel_inline())
        return

    if data == "fj_add":
        user_state[uid] = {"menu": "admin_fj_add_name", "fj_new": {}}
        bot.send_message(
            chat_id,
            "🔐 <b>নতুন Force Join Channel/Group যুক্ত করুন</b>\n\n"
            "প্রথমে চ্যানেল/গ্রুপের নাম (লেবেল) লিখে পাঠান, যেমন: My Channel",
            reply_markup=back_to_main_keyboard(),
        )
        bot.register_next_step_handler(call.message, process_fj_name)
        return

    if data.startswith("fj_remove_"):
        try:
            idx = int(data.replace("fj_remove_", ""))
        except ValueError:
            idx = -1
        channels = get_force_join_channels()
        if 0 <= idx < len(channels):
            removed = channels.pop(idx)
            save_db()
            bot.send_message(
                chat_id,
                f"🗑️ Removed: {removed.get('name', 'Channel')}",
                reply_markup=force_join_admin_inline(),
            )
        else:
            bot.send_message(chat_id, "⚠️ খুঁজে পাওয়া যায়নি।", reply_markup=force_join_admin_inline())
        return


def process_fj_name(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return
    if not text:
        bot.send_message(message.chat.id, "⚠️ সঠিক নাম লিখুন।")
        bot.register_next_step_handler(message, process_fj_name)
        return

    state = user_state.get(uid, {})
    fj_new = state.get("fj_new", {})
    fj_new["name"] = text
    user_state[uid] = {"menu": "admin_fj_add_link", "fj_new": fj_new}
    bot.send_message(
        message.chat.id,
        "🔗 এখন জয়েন করার Invite Link পাঠান।\n"
        "উদাহরণ: https://t.me/your_channel অথবা https://t.me/+AbCdEfGh...",
    )
    bot.register_next_step_handler(message, process_fj_link)


def process_fj_link(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return
    if not text.startswith(("http://", "https://", "t.me/", "@")):
        bot.send_message(message.chat.id, "⚠️ সঠিক লিংক পাঠান (https://t.me/... দিয়ে শুরু)।")
        bot.register_next_step_handler(message, process_fj_link)
        return

    state = user_state.get(uid, {})
    fj_new = state.get("fj_new", {})
    fj_new["link"] = text
    user_state[uid] = {"menu": "admin_fj_add_chatid", "fj_new": fj_new}
    bot.send_message(
        message.chat.id,
        "🆔 এখন চ্যানেল/গ্রুপের ID অথবা @username পাঠান (Membership যাচাই করার জন্য "
        "বট এই চ্যানেল/গ্রুপে অবশ্যই Admin হিসেবে থাকতে হবে)।\n\n"
        "উদাহরণ: @your_channel অথবা -1001234567890",
    )
    bot.register_next_step_handler(message, process_fj_chatid)


def process_fj_chatid(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return
    if not text:
        bot.send_message(message.chat.id, "⚠️ সঠিক ID/username লিখুন।")
        bot.register_next_step_handler(message, process_fj_chatid)
        return

    chat_ref = text
    if not (chat_ref.startswith("@") or chat_ref.startswith("-") or chat_ref.lstrip("-").isdigit()):
        chat_ref = f"@{chat_ref.lstrip('@')}"

    # বট আসলেই ঐ চ্যানেল/গ্রুপে অ্যাক্সেস পায় কিনা যাচাই করা হচ্ছে
    try:
        chat = bot.get_chat(chat_ref)
    except Exception as e:
        bot.send_message(
            message.chat.id,
            "❌ এই চ্যানেল/গ্রুপ খুঁজে পাওয়া যায়নি অথবা বট এখানে নেই।\n"
            f"এরর: {e}\n\n"
            "বটকে আগে ঐ চ্যানেল/গ্রুপে Admin হিসেবে যুক্ত করে আবার ID/username পাঠান, "
            "অথবা ⬅️ Back চাপুন।",
        )
        bot.register_next_step_handler(message, process_fj_chatid)
        return

    state = user_state.get(uid, {})
    fj_new = state.get("fj_new", {})
    fj_new["chat_id"] = chat.id
    fj_new.setdefault("name", chat.title or chat_ref)
    bot_settings.setdefault("force_join_channels", []).append(fj_new)
    save_db()

    bot.send_message(
        message.chat.id,
        f"✅ যুক্ত হয়েছে: <b>{fj_new.get('name')}</b>\n"
        f"🆔 <code>{fj_new['chat_id']}</code>",
        reply_markup=force_join_admin_inline(),
    )
    user_state[uid] = {"menu": "admin_force_join"}


# ---------------------------------------------------------------------------
# ADMIN PANEL: ⚙️ Bot Settings সাব-মেনুর callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("settings_") and is_admin(c.from_user.id))
def cb_bot_settings(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data
    bot.answer_callback_query(call.id)

    if data == "settings_back":
        user_state[uid] = {"menu": "admin_panel"}
        bot.send_message(chat_id, "👮 <b>Admin Panel</b>", reply_markup=admin_panel_inline())
        return

    if data == "settings_toggle_maintenance":
        bot_settings["maintenance_mode"] = not bot_settings["maintenance_mode"]
        save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
        status = "চালু ✅" if bot_settings["maintenance_mode"] else "বন্ধ ❌"
        bot.send_message(
            chat_id,
            f"🛠️ Maintenance Mode এখন {status} করা হয়েছে।",
            reply_markup=bot_settings_inline(),
        )
        return

    if data == "settings_referral_bonus":
        user_state[uid] = {"menu": "admin_settings_referral_bonus"}
        bot.send_message(
            chat_id,
            "🎁 প্রতি রেফারেলে নতুন বোনাস অ্যামাউন্ট লিখে পাঠান।\n"
            f"বর্তমান: {fmt_amount(bot_settings['referral_bonus'])}\n\n"
            "উদাহরণ: 15 অথবা 20.50",
        )
        bot.register_next_step_handler(call.message, process_settings_referral_bonus)
        return

    if data == "settings_deposit_limits":
        user_state[uid] = {"menu": "admin_settings_deposit_limits"}
        bot.send_message(
            chat_id,
            "💳 Min ও Max Deposit লিখে পাঠান, কমা দিয়ে আলাদা করে।\n"
            f"বর্তমান: {bot_settings['min_deposit']} , {bot_settings['max_deposit']}\n\n"
            "উদাহরণ: 50,5000  (সীমা রাখতে না চাইলে 0,0 লিখুন)",
        )
        bot.register_next_step_handler(call.message, process_settings_deposit_limits)
        return

    if data == "settings_deposit_methods":
        user_state[uid] = {"menu": "admin_settings_deposit_methods"}
        bot.send_message(
            chat_id,
            "💳 কমা দিয়ে আলাদা করে Deposit Method গুলো লিখে পাঠান।\n"
            f"বর্তমান: {', '.join(bot_settings['deposit_methods'])}\n\n"
            "উদাহরণ: bKash,Nagad,Rocket,Binance,Manual Bank\n\n"
            f"⚠️ শুধু {', '.join(DEPOSIT_MANUAL_METHODS)} — এই কয়টা মেথড সবসময় ম্যানুয়াল Admin "
            "approval এ যাবে; বাকি যেকোনো নতুন মেথড (যেমন 'Manual Bank') যোগ করলে সেটা সাথে "
            "সাথে অটো-অ্যাপ্রুভ হয়ে যাবে।",
        )
        bot.register_next_step_handler(call.message, process_settings_deposit_methods)
        return

    if data == "settings_support_username":
        user_state[uid] = {"menu": "admin_settings_support_username"}
        bot.send_message(
            chat_id,
            "🆘 Support এর Telegram username লিখে পাঠান (@ সহ বা ছাড়া, দুটোই চলবে)।\n"
            f"বর্তমান: {bot_settings['support_username'] or 'সেট করা নেই'}\n\n"
            "উদাহরণ: @relax1472",
        )
        bot.register_next_step_handler(call.message, process_settings_support_username)
        return

    if data == "settings_method_video":
        user_state[uid] = {"menu": "admin_method_video_list"}
        bot.send_message(
            chat_id,
            "🎥 <b>Method Videos</b>\n\n"
            "বর্তমান ভিডিও বাটনগুলো নিচে দেখানো হলো। ডিলেট করতে চাইলে সংশ্লিষ্ট "
            "🗑️ Remove বাটনে ক্লিক করুন, অথবা নতুন যুক্ত করতে ➕ Add New Video চাপুন।",
            reply_markup=method_video_admin_inline(),
        )
        return


def process_settings_method_video_title(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    if not text:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি Title লিখুন।")
        bot.register_next_step_handler(message, process_settings_method_video_title)
        return

    user_state[uid] = {"menu": "admin_settings_method_video_link", "video_title": text}
    bot.send_message(
        message.chat.id,
        f"🎥 Title: {text}\n\nএখন এই বাটনের জন্য ভিডিও লিংক লিখে পাঠান।\n\nউদাহরণ: https://youtu.be/xxxxxxx",
    )
    bot.register_next_step_handler(message, process_settings_method_video_link)


def process_settings_method_video_link(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    title = state.get("video_title")
    if state.get("menu") != "admin_settings_method_video_link" or not title:
        bot.send_message(message.chat.id, "⚠️ সেশন খুঁজে পাওয়া যায়নি। Bot Settings থেকে আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_bot_settings"}
        return

    if not text.startswith(("http://", "https://")):
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি লিংক লিখুন (http:// বা https:// দিয়ে শুরু)।")
        bot.register_next_step_handler(message, process_settings_method_video_link)
        return

    bot_settings["method_videos"].append({"title": title, "link": text})
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
    bot.send_message(
        message.chat.id,
        f"✅ নতুন Method বাটন যুক্ত হয়েছে!\n\n🎥 Title: {title}\n🔗 Link: {text}",
        reply_markup=method_video_admin_inline(),
    )
    user_state[uid] = {"menu": "admin_method_video_list"}


# ---------------------------------------------------------------------------
# ADMIN PANEL: 🎥 Method Videos সাব-মেনুর callbacks (Add / Remove)
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("mv_") and is_admin(c.from_user.id))
def cb_method_video_admin(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data
    bot.answer_callback_query(call.id)

    if data == "mv_noop":
        return

    if data == "mv_add":
        current = bot_settings["method_videos"]
        current_line = ""
        if current:
            listing = "\n".join(f"• {v['title']}" for v in current)
            current_line = f"বর্তমান ভিডিও বাটনগুলো:\n{listing}\n\n"
        user_state[uid] = {"menu": "admin_settings_method_video_title"}
        bot.send_message(
            chat_id,
            "🎥 ⚙️ Method বাটনে নতুন একটা ভিডিও বাটন যোগ করতে প্রথমে বাটনের Title লিখে পাঠান।\n\n"
            f"{current_line}উদাহরণ: bKash Tutorial",
        )
        bot.register_next_step_handler(call.message, process_settings_method_video_title)
        return

    if data.startswith("mv_remove_"):
        try:
            idx = int(data.replace("mv_remove_", ""))
        except ValueError:
            idx = -1
        videos = bot_settings.get("method_videos") or []
        if 0 <= idx < len(videos):
            removed = videos.pop(idx)
            save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
            bot.send_message(
                chat_id,
                f"🗑️ Removed: {removed.get('title', 'Video')}",
                reply_markup=method_video_admin_inline(),
            )
        else:
            bot.send_message(chat_id, "⚠️ খুঁজে পাওয়া যায়নি।", reply_markup=method_video_admin_inline())
        return

    if data == "mv_back":
        user_state[uid] = {"menu": "admin_bot_settings"}
        bot.send_message(
            chat_id,
            "⚙️ <b>Bot Settings</b>\n\nযে সেটিংসটি বদলাতে চান, তাতে ক্লিক করুন।",
            reply_markup=bot_settings_inline(),
        )
        return


def process_settings_support_username(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    username = text.lstrip("@").strip()
    if not username:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি username লিখুন (যেমন: @relax1472)।")
        bot.register_next_step_handler(message, process_settings_support_username)
        return

    bot_settings["support_username"] = username
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
    bot.send_message(
        message.chat.id,
        f"✅ Support Username আপডেট হয়েছে: @{username}",
        reply_markup=bot_settings_inline(),
    )
    user_state[uid] = {"menu": "admin_bot_settings"}


def process_settings_deposit_methods(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    methods = [p.strip() for p in text.split(",") if p.strip()]
    if not methods:
        bot.send_message(message.chat.id, "⚠️ অন্তত একটা মেথড লিখুন।")
        bot.register_next_step_handler(message, process_settings_deposit_methods)
        return

    bot_settings["deposit_methods"] = methods
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
    for m in methods:
        bot_settings["deposit_numbers"].setdefault(m, "")

    bot.send_message(
        message.chat.id,
        f"✅ Deposit Methods আপডেট হয়েছে: {', '.join(methods)}",
        reply_markup=bot_settings_inline(),
    )
    user_state[uid] = {"menu": "admin_bot_settings"}


def process_settings_referral_bonus(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    try:
        new_bonus = float(text)
    except ValueError:
        new_bonus = None

    if new_bonus is None or new_bonus < 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি সংখ্যা লিখুন (যেমন: 15 অথবা 20.50)।")
        bot.register_next_step_handler(message, process_settings_referral_bonus)
        return

    if new_bonus == int(new_bonus):
        new_bonus = int(new_bonus)

    bot_settings["referral_bonus"] = new_bonus
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
    bot.send_message(
        message.chat.id,
        f"✅ Referral Bonus আপডেট হয়েছে: {fmt_amount(new_bonus)}",
        reply_markup=bot_settings_inline(),
    )
    user_state[uid] = {"menu": "admin_bot_settings"}


def process_settings_deposit_limits(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(p.replace(".", "", 1).isdigit() for p in parts):
        bot.send_message(message.chat.id, "⚠️ সঠিক ফরম্যাটে লিখুন, যেমন: 50,5000")
        bot.register_next_step_handler(message, process_settings_deposit_limits)
        return

    min_dep, max_dep = float(parts[0]), float(parts[1])
    min_dep = int(min_dep) if min_dep == int(min_dep) else min_dep
    max_dep = int(max_dep) if max_dep == int(max_dep) else max_dep

    if max_dep and min_dep > max_dep:
        bot.send_message(message.chat.id, "⚠️ Min, Max এর থেকে বেশি হতে পারবে না। আবার লিখুন।")
        bot.register_next_step_handler(message, process_settings_deposit_limits)
        return

    bot_settings["min_deposit"] = min_dep
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
    bot_settings["max_deposit"] = max_dep
    bot.send_message(
        message.chat.id,
        f"✅ Deposit Limit আপডেট হয়েছে: {min_dep} - {max_dep} BDT",
        reply_markup=bot_settings_inline(),
    )
    user_state[uid] = {"menu": "admin_bot_settings"}


@bot.callback_query_handler(func=lambda c: c.data.startswith("depnum_") and is_admin(c.from_user.id))
def cb_deposit_number_set_start(call):
    method = call.data.replace("depnum_", "")
    bot.answer_callback_query(call.id)
    user_state[call.from_user.id] = {"menu": "admin_deposit_number_value", "method": method}
    current = bot_settings["deposit_numbers"].get(method) or "সেট করা নেই"
    bot.send_message(
        call.message.chat.id,
        f"📮 <b>{method}</b> এর জন্য নতুন Number/Address লিখে পাঠান।\nবর্তমান: {current}",
    )
    bot.register_next_step_handler(call.message, process_deposit_number_value)


def process_deposit_number_value(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    method = state.get("method")
    if not method:
        bot.send_message(message.chat.id, "⚠️ মেথড খুঁজে পাওয়া যায়নি। আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    bot_settings["deposit_numbers"][method] = text   # TODO: DB তে persist করুন
    bot.send_message(
        message.chat.id,
        f"✅ {method} এর Number/Address আপডেট হয়েছে:\n<code>{text}</code>",
        reply_markup=deposit_numbers_inline(),
    )
    user_state[uid] = {"menu": "admin_deposit_numbers"}


def broadcast_to_all_users(text):
    """সব ইউজারকে একটা মেসেজ পাঠায়, কতজনকে পাঠানো গেছে/ব্যর্থ হয়েছে তার কাউন্ট রিটার্ন করে।"""
    sent, failed = 0, 0
    for user_id in list(users.keys()):
        try:
            bot.send_message(user_id, text)
            sent += 1
        except Exception:
            failed += 1  # ইউজার হয়তো বটকে ব্লক করেছে
    return sent, failed


def process_broadcast_message(message):
    """Admin এর লেখা মেসেজ সব ইউজারকে broadcast করে।"""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()

    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    if not text:
        bot.send_message(message.chat.id, "⚠️ মেসেজ খালি রাখা যাবে না। আবার লিখুন।")
        bot.register_next_step_handler(message, process_broadcast_message)
        return

    sent, failed = broadcast_to_all_users(text)
    bot.send_message(
        message.chat.id,
        "✅ <b>Broadcast সম্পন্ন!</b>\n\n"
        f"👥 পাঠানো হয়েছে: {sent} জনকে\n"
        f"❌ ব্যর্থ: {failed} জন",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


@bot.callback_query_handler(func=lambda c: c.data.startswith("uploadprod_") and is_admin(c.from_user.id))
def cb_admin_upload_product_select(call):
    product_key = call.data.replace("uploadprod_", "")
    p = products.get(product_key)
    if not p:
        bot.answer_callback_query(call.id, "Product not found.")
        return

    user_state[call.from_user.id] = {"menu": "admin_upload_file", "product": product_key}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"📤 <b>{p['name']}</b> এর জন্য স্টক ফাইল পাঠান (.txt / .csv / .xlsx)।\n\n"
        "📄 .txt বা .csv এ প্রতি লাইনে একটি এন্ট্রি এই ফরম্যাটে দিন:\n"
        "<code>number|otp_link</code>\n\n"
        "📊 .xlsx এ প্রথম কলামে number, দ্বিতীয় কলামে otp_link দিন।\n\n"
        "OTP লিংক না থাকলে শুধু:\n"
        "<code>number</code>\n\n"
        "ফাইল পাঠানোর পর stock এ যুক্ত করার আগে আপনাকে confirm করতে বলা হবে।",
    )


def _build_unsold_export_workbook():
    """যেসব প্রোডাক্টের stock_list এ এখনও অবিক্রিত (unsold) নাম্বার আছে, তাদের জন্য
    একটা .xlsx ওয়ার্কবুক বানায় — প্রতিটা প্রোডাক্টের জন্য আলাদা শীট, কলাম:
    Number | OTP Link। কোনো প্রোডাক্টেই স্টক না থাকলে None রিটার্ন করে।"""
    from openpyxl import Workbook  # লেজি ইমপোর্ট: শুধু এক্সপোর্ট করার সময়ই দরকার
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)   # ডিফল্ট খালি শীট বাদ দেওয়া হলো

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

    used_titles = set()
    any_stock = False
    for key, p in products.items():
        stock_list = p.get("stock_list") or []
        if not stock_list:
            continue
        any_stock = True

        # Excel শীটের নাম সর্বোচ্চ ৩১ ক্যারেক্টার, ডুপ্লিকেট হলে সাফিক্স যোগ করা হচ্ছে
        base_title = (p.get("name") or key).strip()[:31] or key
        title = base_title
        n = 2
        while title in used_titles:
            suffix = f" ({n})"
            title = base_title[: 31 - len(suffix)] + suffix
            n += 1
        used_titles.add(title)

        ws = wb.create_sheet(title=title)
        ws.append(["Number", "OTP Link"])
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
        for item in stock_list:
            ws.append([item.get("number", ""), item.get("otp_link", "N/A")])
        ws.column_dimensions["A"].width = 25
        ws.column_dimensions["B"].width = 40

    if not any_stock:
        return None
    return wb


def parse_stock_from_xlsx(file_bytes):
    """xlsx ফাইলের প্রথম শীট থেকে (number, otp_link) কলাম দুটো রিড করে।
    হেডার-জাতীয় লাইন (number/phone ইত্যাদি) স্বয়ংক্রিয়ভাবে স্কিপ হয়।"""
    from openpyxl import load_workbook  # লেজি ইমপোর্ট: শুধু .xlsx আপলোড হলেই দরকার

    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws = wb.active
    items = []
    for row in ws.iter_rows(values_only=True):
        if not row:
            continue
        number = row[0]
        otp_link = row[1] if len(row) > 1 else None
        if number is None:
            continue
        number = str(number).strip()
        if not number or number.lower() in ("number", "phone", "phone number", "otp_link", "otp"):
            continue
        otp_link = str(otp_link).strip() if otp_link not in (None, "") else "N/A"
        items.append({"number": number, "otp_link": otp_link})
    return items


@bot.message_handler(content_types=["document"])
def handle_document_upload(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    state = user_state.get(uid, {})
    menu = state.get("menu")

    if menu == "admin_import_db_wait_file":
        handle_db_import_file(message)
        return

    if menu != "admin_upload_file":
        return

    product_key = state.get("product")
    p = products.get(product_key)
    if not p:
        bot.send_message(message.chat.id, "⚠️ প্রোডাক্ট খুঁজে পাওয়া যায়নি। Admin Panel থেকে আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith((".txt", ".csv", ".xlsx")):
        bot.send_message(message.chat.id, "⚠️ শুধুমাত্র .txt, .csv বা .xlsx ফাইল সাপোর্ট করে। আবার পাঠান।")
        return

    # ফাইল ডাউনলোড ও parse করা হচ্ছে - এখনো stock এ যুক্ত হয়নি, শুধু preview/confirmation এর জন্য
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল ডাউনলোড করতে সমস্যা হয়েছে: {e}")
        return

    parsed_items = []
    try:
        if file_name.lower().endswith(".xlsx"):
            parsed_items = parse_stock_from_xlsx(downloaded)
        else:
            text = downloaded.decode("utf-8", errors="ignore")
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if "|" in line:
                    number, otp_link = line.split("|", 1)
                elif "," in line:
                    number, otp_link = line.split(",", 1)
                else:
                    number, otp_link = line, "N/A"
                number = number.strip()
                otp_link = otp_link.strip() or "N/A"
                if number:
                    parsed_items.append({"number": number, "otp_link": otp_link})
    except ImportError:
        bot.send_message(
            message.chat.id,
            "❌ .xlsx ফাইল পড়ার জন্য সার্ভারে openpyxl লাইব্রেরি ইনস্টল নেই।\n"
            "ইনস্টল করুন: pip install openpyxl",
        )
        return
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল পার্স করতে সমস্যা হয়েছে: {e}")
        return

    if not parsed_items:
        bot.send_message(message.chat.id, "⚠️ ফাইলে কোনো valid লাইন পাওয়া যায়নি। ফরম্যাট চেক করে আবার পাঠান।")
        return

    # price আর জিজ্ঞেস করা হয় না — প্রোডাক্টের নিজস্ব price (Admin Panel থেকে সেট করা)
    # ব্যবহার হয়, তাই ফাইল parse হওয়ার সাথে সাথেই সরাসরি Confirm ধাপে যাওয়া হচ্ছে।
    user_state[uid] = {
        "menu": "admin_upload_confirm",
        "product": product_key,
        "pending_items": parsed_items,
    }

    preview = "\n".join(f"• {it['number']} | {it['otp_link']}" for it in parsed_items[:3])
    if len(parsed_items) > 3:
        preview += f"\n...আরও {len(parsed_items) - 3} টি"

    current_stock = len(p.setdefault("stock_list", []))
    bot.send_message(
        message.chat.id,
        "🔎 <b>Confirm Stock Upload</b>\n\n"
        f"📦 প্রোডাক্ট: {p['name']}\n"
        f"📄 ফাইল: {file_name}\n"
        f"🔢 ফাইলে পাওয়া গেছে: {len(parsed_items)} পিস\n\n"
        f"প্রিভিউ:\n{preview}\n\n"
        f"📊 বর্তমান স্টক: {current_stock} → Confirm করলে হবে: {current_stock + len(parsed_items)}\n\n"
        "এই এন্ট্রিগুলো stock এ যুক্ত করতে চান?",
        reply_markup=upload_confirm_inline(),
    )


@bot.callback_query_handler(
    func=lambda c: c.data in ("stockup_confirm", "stockup_cancel") and is_admin(c.from_user.id)
)
def cb_admin_upload_confirm(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    state = user_state.get(uid, {})
    bot.answer_callback_query(call.id)

    if state.get("menu") != "admin_upload_confirm":
        bot.send_message(chat_id, "⚠️ কোনো pending upload নেই। আগে একটা ফাইল পাঠান।")
        return

    if call.data == "stockup_cancel":
        bot.send_message(
            chat_id,
            "❌ Upload বাতিল করা হয়েছে। কিছুই stock এ যুক্ত হয়নি।",
            reply_markup=admin_panel_inline(),
        )
        user_state[uid] = {"menu": "admin_panel"}
        return

    # stockup_confirm
    product_key = state.get("product")
    pending_items = state.get("pending_items", [])
    p = products.get(product_key)

    if not p or not pending_items:
        bot.send_message(chat_id, "⚠️ Pending ডেটা খুঁজে পাওয়া যায়নি। আবার আপলোড করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    stock_list = p.setdefault("stock_list", [])
    stock_list.extend(pending_items)
    p["stock"] = len(stock_list)
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    stock_added_text = (
        "🔔 <b>New Stock Added!</b>\n\n"
        f"📱 Product: {p['name']}\n"
        f"📦 Added: {len(pending_items)} numbers\n"
        f"💵 Price: {fmt_amount(p['price'])} (প্রতি পিস)\n"
        f"📊 Total {p['name']} Stock: {p['stock']}"
    )
    bot.send_message(chat_id, stock_added_text)
    bot.send_message(
        chat_id,
        "📢 এই স্টক আপডেট সব ইউজারকে broadcast করতে চান?",
        reply_markup=stock_broadcast_confirm_inline(),
    )
    user_state[uid] = {"menu": "admin_panel", "pending_broadcast_text": stock_added_text}


# ---------------------------------------------------------------------------
# ADMIN: Price পরিবর্তন (Set Price)
# ---------------------------------------------------------------------------
@bot.callback_query_handler(
    func=lambda c: c.data in ("stockbroadcast_yes", "stockbroadcast_no") and is_admin(c.from_user.id)
)
def cb_stock_broadcast(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    state = user_state.get(uid, {})
    text = state.get("pending_broadcast_text")

    if call.data == "stockbroadcast_no" or not text:
        bot.send_message(chat_id, "❌ Broadcast করা হয়নি।", reply_markup=admin_panel_inline())
        user_state[uid] = {"menu": "admin_panel"}
        return

    sent, failed = broadcast_to_all_users(text)
    bot.send_message(
        chat_id,
        "✅ <b>Broadcast সম্পন্ন!</b>\n\n"
        f"👥 পাঠানো হয়েছে: {sent} জনকে\n"
        f"❌ ব্যর্থ: {failed} জন",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


@bot.callback_query_handler(func=lambda c: c.data.startswith("priceprod_") and is_admin(c.from_user.id))
def cb_admin_price_product_select(call):
    product_key = call.data.replace("priceprod_", "")
    p = products.get(product_key)
    if not p:
        bot.answer_callback_query(call.id, "Product not found.")
        return

    user_state[call.from_user.id] = {"menu": "admin_set_price", "product": product_key}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"💰 <b>{p['name']}</b> এর জন্য নতুন price লিখে পাঠান।\n"
        f"বর্তমান price: {fmt_amount(p['price'])}\n\n"
        "উদাহরণ: 50 অথবা 49.99",
    )
    bot.register_next_step_handler(call.message, process_set_price)


def process_set_price(message):
    """Admin এর দেওয়া নতুন price validate করে products dict এ সেট করে।"""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()

    # ইউজার মাঝপথে Back/Menu এ চলে যেতে চাইলে next-step এর সাথে conflict এড়ানো হচ্ছে
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)
    if not p:
        bot.send_message(message.chat.id, "⚠️ প্রোডাক্ট খুঁজে পাওয়া যায়নি। Admin Panel থেকে আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    try:
        new_price = float(text)
    except ValueError:
        new_price = None

    if new_price is None or new_price <= 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি price সংখ্যায় লিখুন (যেমন: 50 অথবা 49.99)।")
        bot.register_next_step_handler(message, process_set_price)
        return

    if new_price == int(new_price):
        new_price = int(new_price)

    old_price = p["price"]
    p["price"] = new_price
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    bot.send_message(
        message.chat.id,
        "✅ <b>Price আপডেট হয়েছে!</b>\n\n"
        f"📦 প্রোডাক্ট: {p['name']}\n"
        f"💵 পুরাতন Price: {fmt_amount(old_price)}\n"
        f"💵 নতুন Price: {fmt_amount(p['price'])}",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


# ---------------------------------------------------------------------------
# ADMIN: Dollar Rate সেট করা (fmt_amount সব জায়গায় এই রেট ব্যবহার করে)
# ---------------------------------------------------------------------------
def process_set_usd_rate(message):
    """Admin এর দেওয়া নতুন USD->BDT রেট validate করে bot_settings এ সেট করে।"""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()

    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    try:
        new_rate = float(text)
    except ValueError:
        new_rate = None

    if new_rate is None or new_rate <= 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি রেট সংখ্যায় লিখুন (যেমন: 122 অথবা 121.50)।")
        bot.register_next_step_handler(message, process_set_usd_rate)
        return

    if new_rate == int(new_rate):
        new_rate = int(new_rate)

    old_rate = bot_settings.get("usd_rate") or 0
    bot_settings["usd_rate"] = new_rate
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    bot.send_message(
        message.chat.id,
        "✅ <b>Dollar Rate আপডেট হয়েছে!</b>\n\n"
        f"💱 পুরাতন রেট: {(f'1 USD = {old_rate} BDT') if old_rate else 'সেট করা নেই'}\n"
        f"💱 নতুন রেট: 1 USD = {new_rate} BDT",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


# ---------------------------------------------------------------------------
# ADMIN PANEL: 💵 Add/Remove Balance — Admin একটা user_id দিবে, তারপর
# +amount বা -amount দিয়ে সেই ইউজারের ব্যালেন্স বাড়াবে/কমাবে।
# ---------------------------------------------------------------------------
def process_balance_edit_uid(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    try:
        target_id = int(text)
    except ValueError:
        target_id = None

    if target_id is None or target_id not in users:
        bot.send_message(
            message.chat.id,
            "⚠️ এই User ID খুঁজে পাওয়া যায়নি। সঠিক Telegram User ID লিখে আবার পাঠান।",
        )
        bot.register_next_step_handler(message, process_balance_edit_uid)
        return

    target = users[target_id]
    user_state[uid] = {"menu": "admin_balance_edit_wait_amount", "target_uid": target_id}
    bot.send_message(
        message.chat.id,
        f"👤 User: {target.get('full_name', '—')} ({target.get('username', '—')}) | <code>{target_id}</code>\n"
        f"💰 বর্তমান Balance: {fmt_amount(target.get('balance', 0))}\n\n"
        "কত টাকা যোগ/বিয়োগ করতে চান লিখে পাঠান।\n"
        "যোগ করতে: <code>+100</code>\nবিয়োগ করতে: <code>-100</code>",
        reply_markup=back_to_main_keyboard(),
    )
    bot.register_next_step_handler(message, process_balance_edit_amount)


def process_balance_edit_amount(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    if state.get("menu") != "admin_balance_edit_wait_amount" or not state.get("target_uid"):
        bot.send_message(message.chat.id, "⚠️ সেশন খুঁজে পাওয়া যায়নি। Admin Panel থেকে আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    try:
        delta = float(text.replace("+", ""))
    except ValueError:
        delta = None

    if delta is None or delta == 0:
        bot.send_message(
            message.chat.id,
            "⚠️ সঠিক একটি amount লিখুন (যেমন: +100 অথবা -50)।",
        )
        bot.register_next_step_handler(message, process_balance_edit_amount)
        return

    target_id = state["target_uid"]
    target = users.get(target_id)
    if not target:
        bot.send_message(message.chat.id, "⚠️ এই ইউজার আর খুঁজে পাওয়া যাচ্ছে না।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    old_balance = target.get("balance", 0)
    new_balance = old_balance + delta
    target["balance"] = new_balance
    save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)

    action_word = "যোগ" if delta > 0 else "বিয়োগ"
    bot.send_message(
        message.chat.id,
        "✅ <b>Balance আপডেট হয়েছে!</b>\n\n"
        f"👤 User: {target.get('full_name', '—')} | <code>{target_id}</code>\n"
        f"{'➕' if delta > 0 else '➖'} {action_word}: {fmt_amount(abs(delta))}\n"
        f"💰 পুরাতন Balance: {fmt_amount(old_balance)}\n"
        f"💰 নতুন Balance: {fmt_amount(new_balance)}",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}

    try:
        bot.send_message(
            target_id,
            ("💵 <b>আপনার ব্যালেন্সে যোগ করা হয়েছে!</b>\n\n" if delta > 0 else
             "💵 <b>আপনার ব্যালেন্স থেকে কাটা হয়েছে!</b>\n\n") +
            f"{'➕' if delta > 0 else '➖'} Amount: {fmt_amount(abs(delta))}\n"
            f"💰 নতুন Balance: {fmt_amount(new_balance)}",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ADMIN PANEL: 🔎 User Info — Admin একটা user_id দিলে সেই ইউজারের সব তথ্য
# (প্রোফাইল + Deposits + Orders সামারি) একসাথে দেখায়।
# ---------------------------------------------------------------------------
def _user_deposits(target_id):
    """একজন ইউজারের সব ডিপোজিট, সবচেয়ে নতুনটা আগে।"""
    return sorted(
        (d for d in deposits.values() if d.get("user_id") == target_id),
        key=lambda d: d.get("id", 0),
        reverse=True,
    )


def _user_orders(target_id):
    """একজন ইউজারের সব অর্ডার, সবচেয়ে নতুনটা আগে।"""
    return sorted(
        (o for o in orders.values() if o.get("user_id") == target_id),
        key=lambda o: o.get("date", ""),
        reverse=True,
    )


def admin_user_info_text(target_id, target):
    """Admin এর জন্য একজন নির্দিষ্ট ইউজারের সম্পূর্ণ তথ্য (প্রোফাইল + Deposits +
    Orders সামারি, সর্বশেষ ৫টা করে দেখানো হয়) তৈরি করে।"""
    referred_by_line = ""
    referred_by = target.get("referred_by")
    if referred_by:
        ref_user = users.get(referred_by)
        ref_name = ref_user.get("full_name", "—") if ref_user else "—"
        referred_by_line = f"🔗 <b>Referred By:</b> <code>{referred_by}</code> ({ref_name})\n"

    status_emoji = {"approved": "✅", "pending": "⏳", "rejected": "❌"}

    dep_list = _user_deposits(target_id)
    total_deposited = sum(d.get("amount", 0) for d in dep_list if d.get("status") == "approved")
    if dep_list:
        dep_lines = "".join(
            f"{status_emoji.get(d.get('status'), '•')} DEP-{d['id']} | {d.get('method', '—')} | "
            f"{fmt_amount(d.get('amount', 0))} | {d.get('status', '—')} | {d.get('date', '—')}\n"
            for d in dep_list[:5]
        )
    else:
        dep_lines = "কোনো ডিপোজিট নেই।\n"
    more_dep = f"…আরও {len(dep_list) - 5}টি পুরোনো ডিপোজিট আছে।\n" if len(dep_list) > 5 else ""

    order_list = _user_orders(target_id)
    total_spent_all = sum(o.get("total", 0) for o in order_list)
    if order_list:
        order_lines = "".join(
            f"🧾 {o['order_id']} | {o.get('product_name', '—')} x{o.get('qty', 1)} | "
            f"{fmt_amount(o.get('total', 0))} | {o.get('date', '—')}\n"
            for o in order_list[:5]
        )
    else:
        order_lines = "কোনো অর্ডার নেই।\n"
    more_order = f"…আরও {len(order_list) - 5}টি পুরোনো অর্ডার আছে।\n" if len(order_list) > 5 else ""

    return (
        "🔎 <b>User Info</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
        f"👤 <b>Full Name:</b> <code>{target.get('full_name', '—')}</code>\n"
        f"📝 <b>Username:</b> <code>{target.get('username', '—')}</code>\n"
        f"💰 <b>Balance:</b> <code>{fmt_amount(target.get('balance', 0))}</code>\n"
        f"📊 <b>Total Purchased:</b> <code>{target.get('total_purchased', 0)}</code>\n"
        f"💸 <b>Today Spent:</b> <code>{fmt_amount(target.get('today_spent', 0))}</code>\n"
        f"💳 <b>Today Deposit:</b> <code>{fmt_amount(target.get('today_deposit', 0))}</code>\n"
        f"👥 <b>Referrals:</b> <code>{target.get('referrals', 0)}</code>\n"
        f"🎁 <b>Referral Earned:</b> <code>{fmt_amount(target.get('earned', 0))}</code>\n"
        f"{referred_by_line}\n"
        f"💰 <b>Deposits</b> (মোট: {len(dep_list)} | Approved Total: {fmt_amount(total_deposited)})\n"
        f"{dep_lines}{more_dep}\n"
        f"🧾 <b>Orders</b> (মোট: {len(order_list)} | Total Spent: {fmt_amount(total_spent_all)})\n"
        f"{order_lines}{more_order}"
    )


def process_admin_user_info_uid(message):
    """Admin এর দেওয়া User ID validate করে সেই ইউজারের সব তথ্য দেখায়।"""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    try:
        target_id = int(text)
    except ValueError:
        target_id = None

    if target_id is None or target_id not in users:
        bot.send_message(
            message.chat.id,
            "⚠️ এই User ID খুঁজে পাওয়া যায়নি। সঠিক Telegram User ID লিখে আবার পাঠান।",
        )
        bot.register_next_step_handler(message, process_admin_user_info_uid)
        return

    target = users[target_id]
    bot.send_message(
        message.chat.id,
        admin_user_info_text(target_id, target),
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


# ---------------------------------------------------------------------------
# ADMIN: DB Export / Import (Backup ও Restore) — এখনো in-memory ডেটা, তাই
# bot restart হলে সব হারিয়ে যায়; এই ফিচার দিয়ে ম্যানুয়ালি Export/Import করা
# যাবে (TODO: ভবিষ্যতে persistent DB এলে auto-load এর দরকার থাকবে না)।
# ---------------------------------------------------------------------------
def _build_db_export_payload():
    return {
        "version": 1,
        "exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "users": users,
        "products": products,
        "orders": orders,
        "deposits": deposits,
        "sms_log": sms_log,
        "bot_settings": bot_settings,
        "next_deposit_id": _deposit_id_counter[0],
        "next_sms_id": _sms_id_counter[0],
    }


def _restore_db_from_payload(payload):
    """ব্যাকআপ payload থেকে in-memory সব ডেটা রিপ্লেস করে। users/deposits এর
    key (user_id/deposit_id) JSON এ string হয়ে যায়, তাই আবার int এ কনভার্ট
    করা হচ্ছে।"""
    new_users = {int(k): v for k, v in (payload.get("users") or {}).items()}
    new_deposits = {int(k): v for k, v in (payload.get("deposits") or {}).items()}
    new_orders = dict(payload.get("orders") or {})
    new_products = payload.get("products")
    new_sms_log = list(payload.get("sms_log") or [])
    new_settings = payload.get("bot_settings")

    users.clear()
    users.update(new_users)

    orders.clear()
    orders.update(new_orders)

    deposits.clear()
    deposits.update(new_deposits)

    sms_log.clear()
    sms_log.extend(new_sms_log)

    if isinstance(new_products, dict):
        products.clear()
        products.update(new_products)

    if isinstance(new_settings, dict):
        bot_settings.update(new_settings)
        bot_settings.setdefault("deposit_methods", ["bKash", "Nagad", "Rocket", "Binance"])
        bot_settings.setdefault("deposit_numbers", {})
        bot_settings.setdefault("force_join_channels", [])

    if "next_deposit_id" in payload:
        try:
            _deposit_id_counter[0] = int(payload["next_deposit_id"])
        except (TypeError, ValueError):
            pass
    if "next_sms_id" in payload:
        try:
            _sms_id_counter[0] = int(payload["next_sms_id"])
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# ✅ PERSISTENT DATABASE (এখন আর শুধু in-memory না — ডিস্কে JSON ফাইলে অটো-সেভ)
# --------------------------------------------------------------------------
# আগে সব ডেটা (users/products/orders/deposits/sms_log/bot_settings) শুধু RAM এ
# থাকতো -> bot restart হলে সব হারিয়ে যেতো। এখন উপরের _build_db_export_payload()/
# _restore_db_from_payload() ফাংশন দুটো ব্যবহার করেই পুরো ডেটাসেট নিয়মিত ডিস্কে
# (DB_FILE_PATH) সেভ হয়, আর বট চালু হওয়ার সময় সেখান থেকে অটোমেটিক লোড হয়ে যায়।
# Railway তে persistent থাকতে হলে এই ফাইলের path একটা attached Volume এ রাখুন
# (env var DB_FILE_PATH দিয়ে কাস্টম path সেট করা যাবে)।
DB_FILE_PATH = os.environ.get("DB_FILE_PATH", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bot_data.json"
)
_db_save_lock = threading.Lock()


def save_db():
    """সব in-memory ডেটা ডিস্কে JSON ফাইলে সেভ করে (atomic write: আগে .tmp ফাইলে
    লিখে তারপর আসল ফাইলের নামে rename করা হয়, যাতে সেভের মাঝপথে বট ক্র্যাশ
    করলেও ফাইল করাপ্ট না হয়ে যায়)।"""
    try:
        payload = _build_db_export_payload()
        with _db_save_lock:
            tmp_path = DB_FILE_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, DB_FILE_PATH)
    except Exception as e:
        print(f"⚠️ save_db() ব্যর্থ হয়েছে: {e}")


def load_db():
    """বট চালু হওয়ার সময় ডিস্কে আগে সেভ করা DB_FILE_PATH ফাইল (থাকলে) থেকে সব
    ডেটা অটোমেটিক লোড করে। ফাইল না থাকলে (প্রথমবার রান) ফ্রেশ/খালি ডেটা দিয়ে
    শুরু হবে।"""
    if not os.path.exists(DB_FILE_PATH):
        print(f"ℹ️  কোনো আগের DB ফাইল পাওয়া যায়নি ({DB_FILE_PATH}) — ফ্রেশ ডেটা দিয়ে শুরু হচ্ছে।")
        return
    try:
        with open(DB_FILE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        _restore_db_from_payload(payload)
        print(
            f"✅ DB লোড হয়েছে -> {DB_FILE_PATH} "
            f"(Users: {len(users)}, Orders: {len(orders)}, Deposits: {len(deposits)})"
        )
    except Exception as e:
        print(f"⚠️ load_db() ব্যর্থ হয়েছে, ফ্রেশ ডেটা দিয়ে শুরু হচ্ছে: {e}")


def _autosave_loop():
    """প্রতি ১০ সেকেন্ড পরপর ব্যাকগ্রাউন্ডে অটোমেটিক DB সেভ করে, যাতে কোনো
    জায়গায় সরাসরি save_db() কল করতে ভুলে গেলেও ডেটা বেশিক্ষণ (max ১০ সেকেন্ড)
    আন-সেভড না থাকে।"""
    while True:
        time.sleep(10)
        save_db()


def start_persistent_db():
    """লোড + ব্যাকগ্রাউন্ড অটোসেভ থ্রেড চালু করে। Polling ও Webhook — দুই মোডেই
    রান হওয়ার আগে একবার কল হয়।"""
    load_db()
    threading.Thread(target=_autosave_loop, daemon=True).start()


def handle_db_import_file(message):
    """Admin এর পাঠানো .json ব্যাকআপ ফাইল ডাউনলোড/পার্স করে, Restore করার আগে
    কাউন্টসহ Confirm/Cancel বাটন দেখায় — সরাসরি ডেটা রিপ্লেস করে না।"""
    uid = message.from_user.id
    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".json"):
        bot.send_message(message.chat.id, "⚠️ শুধুমাত্র .json ব্যাকআপ ফাইল সাপোর্ট করে। আবার পাঠান।")
        return

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        payload = json.loads(downloaded.decode("utf-8"))
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল পড়তে/পার্স করতে সমস্যা হয়েছে: {e}")
        return

    if not isinstance(payload, dict) or "users" not in payload:
        bot.send_message(message.chat.id, "⚠️ এটা এই বটের সঠিক ব্যাকআপ ফাইল বলে মনে হচ্ছে না।")
        return

    user_state[uid] = {"menu": "admin_import_db_confirm", "pending_import": payload}

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Confirm & Restore", callback_data="dbimport_confirm"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="dbimport_cancel"),
    )
    bot.send_message(
        message.chat.id,
        "🔎 <b>Confirm DB Import</b>\n\n"
        f"📄 ফাইল: {file_name}\n"
        f"👥 Users: {len(payload.get('users') or {})}\n"
        f"🧾 Orders: {len(payload.get('orders') or {})}\n"
        f"💰 Deposits: {len(payload.get('deposits') or {})}\n"
        f"📩 SMS Log: {len(payload.get('sms_log') or [])}\n\n"
        "⚠️ Confirm করলে বর্তমান সব ডেটা মুছে এই ব্যাকআপ দিয়ে রিপ্লেস হয়ে যাবে। এই কাজ Undo করা যাবে না।",
        reply_markup=kb,
    )


@bot.callback_query_handler(
    func=lambda c: c.data in ("dbimport_confirm", "dbimport_cancel") and is_admin(c.from_user.id)
)
def cb_db_import_confirm(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    state = user_state.get(uid, {})
    bot.answer_callback_query(call.id)

    if state.get("menu") != "admin_import_db_confirm":
        bot.send_message(chat_id, "⚠️ কোনো pending import নেই। আগে একটা ব্যাকআপ (.json) ফাইল পাঠান।")
        return

    if call.data == "dbimport_cancel":
        bot.send_message(
            chat_id,
            "❌ Import বাতিল করা হয়েছে। কিছুই পরিবর্তন হয়নি।",
            reply_markup=admin_panel_inline(),
        )
        user_state[uid] = {"menu": "admin_panel"}
        return

    payload = state.get("pending_import") or {}
    try:
        _restore_db_from_payload(payload)
        save_db()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Restore করতে সমস্যা হয়েছে: {e}")
        user_state[uid] = {"menu": "admin_panel"}
        return

    bot.send_message(
        chat_id,
        "✅ <b>DB Import সম্পন্ন!</b>\n\n"
        f"👥 Users: {len(users)}\n"
        f"🧾 Orders: {len(orders)}\n"
        f"💰 Deposits: {len(deposits)}\n"
        f"📩 SMS Log: {len(sms_log)}",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


# ---------------------------------------------------------------------------
# FALLBACK
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    # TODO: register_next_step_handler গুলো active থাকলে সেগুলো এখানে conflict না করার
    #       ব্যাপারে খেয়াল রাখবেন
    if require_force_join(message):
        return
    bot.send_message(
        message.chat.id,
        "❓ বুঝতে পারিনি। নিচের মেনু থেকে বেছে নিন।",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# SMS AUTO-DEPOSIT: পেমেন্ট SMS/নোটিফিকেশন পার্স করা ও pending deposit এর
# সাথে ম্যাচ করা (bKash/Nagad/Rocket/Binance)
# ---------------------------------------------------------------------------
_SMS_INCOMING_HINTS = (
    "you have received", "received tk", "received taka", "cash in",
    "money received", "credited", "পেমেন্ট", "রিসিভ",
)
_SMS_OUTGOING_HINTS = (
    "you have sent", "payment sent", "cash out", "withdrawn", "debited",
    "you sent", "send money",
)

_AMOUNT_RE = re.compile(r"(?:tk|taka|bdt)\.?\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_TRXID_RE = re.compile(
    r"(?:trx\s*id|txn\s*id|transaction\s*id|trxid|txnid|trx\s*no\.?|txn\s*no\.?|ref(?:erence)?\s*id)"
    r"[\s:\-]*([a-z0-9]{4,})",
    re.IGNORECASE,
)
# 🪙 Binance ওয়ালেট নোটিফিকেশনে TrxID থাকে না, বরং sender username থাকে (স্পেসসহ
# হতে পারে) — উদাহরণ: "You have received a payment of 0.1 USDT from Garth
# Mantifel wpgu on 2026-09-15 04:01:53(UTC)"
_BINANCE_RECEIVED_RE = re.compile(
    r"received\s+(?:a\s+)?payment\s+of\s*([\d]+(?:\.\d+)?)\s*([a-z]{2,10})\s+from\s+(.+?)\s+on\s+\d{4}-\d{1,2}-\d{1,2}",
    re.IGNORECASE,
)
_SENDER_METHOD_CODES = {"16216": "Rocket"}   # Rocket এর অফিসিয়াল sender short-code
_SENDER_METHOD_NAMES = {"nagad": "Nagad"}


def _normalize_binance_name(name):
    return re.sub(r"\s+", " ", name.strip())


def _id_label(method):
    return "Binance Username" if method == "Binance" else "TrxID"


def _amount_unit(method):
    return "USDT" if method == "Binance" else "BDT"


def _method_from_sender(sender):
    if not sender:
        return ""
    sender_l = sender.strip().lower()
    for name, method in _SENDER_METHOD_NAMES.items():
        if name in sender_l:
            return method
    s = re.sub(r"[^0-9]", "", sender)
    for code, method in _SENDER_METHOD_CODES.items():
        if s == code or s.endswith(code):
            return method
    return ""


def parse_payment_sms(text, hint_method="", sender=""):
    """একটা raw SMS টেক্সট থেকে method/amount/trx_id বের করার চেষ্টা করে।
    ইনকামিং পেমেন্ট SMS না মনে হলে, বা amount/trx_id না পেলে None রিটার্ন করে।"""
    if not text:
        return None
    t = text.strip()
    tl = t.lower()

    if any(h in tl for h in _SMS_OUTGOING_HINTS) and not any(h in tl for h in _SMS_INCOMING_HINTS):
        return None  # টাকা পাঠানো/ক্যাশ-আউটের SMS, ডিপোজিটের জন্য না

    bm = _BINANCE_RECEIVED_RE.search(t)
    if bm:
        try:
            b_amount = float(bm.group(1))
        except ValueError:
            b_amount = None
        b_name = _normalize_binance_name(bm.group(3))
        if b_amount is not None and b_name:
            return {"method": "Binance", "amount": b_amount, "trx_id": b_name}
        return None

    method = _method_from_sender(sender)
    if not method:
        if "bkash" in tl:
            method = "bKash"
        elif "nagad" in tl:
            method = "Nagad"
        elif "rocket" in tl or "dbbl" in tl:
            method = "Rocket"
        if not method and re.search(r"(?<!\d)16216(?!\d)", t):
            method = "Rocket"
    if not method and hint_method:
        method = hint_method.strip()

    amount = None
    m = _AMOUNT_RE.search(t)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            amount = None

    trx_id = None
    m2 = _TRXID_RE.search(t)
    if m2:
        trx_id = m2.group(1).strip().upper()

    if amount is None or not trx_id:
        return None
    return {"method": method, "amount": amount, "trx_id": trx_id}


def _methods_match(a, b):
    """কোনোটা খালি থাকলে মিল ধরা হয় না — method যাচাই না করে auto-approve করা যাবে না।"""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    return bool(a) and bool(b) and a == b


def _amounts_match(method, a, b):
    if a is None or b is None:
        return False
    eps = 0.0005 if method == "Binance" else 0.01
    return abs(a - b) < eps


def _auto_approve_deposit_with_sms(dep, sms):
    """dep + sms দুটোকেই approved/used হিসেবে মার্ক করে ব্যালেন্স যোগ করে।

    🔒 Race-condition fix: dep['status']=='pending' ও sms['is_used']==False কিনা
    চেক করা থেকে শুরু করে approved/used সেট করা পর্যন্ত পুরোটা _deposit_lock দিয়ে
    atomic রাখা হয়েছে — একই deposit/SMS দুইবার (যেমন duplicate SMS webhook কল বা
    একই সময়ে ম্যানুয়াল Approve) ম্যাচ হয়ে দুইবার ব্যালেন্স যোগ হওয়া এতে ঠেকানো যায়।

    🛡️ Data-safety fix: ইউজার রেকর্ড খুঁজে না পেলে deposit approved মার্ক না করে
    (ব্যালেন্স-বিহীন approved অবস্থা এড়াতে) pending-ই রাখা হয় এবং Admin-কে সতর্ক
    করা হয়।"""
    with _deposit_lock:
        if dep["status"] != "pending" or sms["is_used"]:
            return False

        u = users.get(dep["user_id"])
        if u is None:
            for admin_id in ADMIN_IDS:
                try:
                    bot.send_message(
                        admin_id,
                        "⚠️ <b>SMS ম্যাচ হয়েছে কিন্তু ইউজার খুঁজে পাওয়া যায়নি!</b>\n\n"
                        f"🆔 DEP-{dep['id']} | 👤 <code>{dep['user_id']}</code>\n"
                        "ব্যালেন্স যোগ করা হয়নি, deposit এখনও 'pending' আছে। ম্যানুয়ালি চেক করুন।",
                    )
                except Exception:
                    pass
            return False

        dep["status"] = "approved"
        sms["is_used"] = True
        sms["used_by_deposit_id"] = dep["id"]
        u["balance"] += dep["amount"]
        u["today_deposit"] += dep["amount"]
        save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
        _clear_deposit_admin_buttons(dep)   # আগে দেখানো থাকলে Pending Deposits লিস্টের বাটনও সরানো হলো

    try:
        bot.send_message(
            dep["user_id"],
            "✅ <b>Deposit Auto-Approved!</b>\n\n"
            f"🆔 Request: DEP-{dep['id']}\n"
            f"💳 Method: {dep['method']}\n"
            f"💰 +{fmt_amount(dep['amount'])} added\n"
            f"💰 New Balance: {fmt_amount(u['balance'])}",
        )
    except Exception:
        pass
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                "🤖 <b>Auto-Approved Deposit (SMS matched)</b>\n\n"
                f"🆔 DEP-{dep['id']} | 👤 <code>{dep['user_id']}</code>\n"
                f"💳 {dep['method']} | 💰 {fmt_amount(dep['amount'])}\n"
                f"🔑 {_id_label(dep['method'])}: <code>{dep['trx_id']}</code>\n"
                f"📩 Matched SMS #{sms['id']}",
            )
        except Exception:
            pass
    return True


def _auto_reject_mismatched_deposit(dep, sms, method_ok, amount_ok):
    """TrxID মিলেছে কিন্তু Method/Amount মিলেনি — ইউজার ভুল তথ্য দিয়েছে, তাই
    pending না রেখে সাথে সাথে reject করে ইউজারকে জানানো হচ্ছে।
    🔒 এখানেও status চেক + সেট করার অংশটা _deposit_lock দিয়ে atomic রাখা হয়েছে।"""
    with _deposit_lock:
        if dep["status"] != "pending":
            return
        dep["status"] = "rejected"
        save_db()   # ✅ ডিস্কে persist করা হলো (persistent DB)
        _clear_deposit_admin_buttons(dep)

    mismatch_lines = []
    if not method_ok:
        mismatch_lines.append(f"💳 Method মিলছে না — Deposit: {dep['method']} vs SMS: {sms['method'] or '—'}")
    if not amount_ok:
        mismatch_lines.append(
            f"💰 Amount মিলছে না — Deposit: {dep['amount']} vs SMS: {sms['amount']} {_amount_unit(sms['method'])}"
        )

    try:
        bot.send_message(
            dep["user_id"],
            "❌ <b>আপনার দেওয়া তথ্য সঠিক নয়!</b>\n\n"
            f"🆔 Request: DEP-{dep['id']}\n\n"
            "আপনার দেওয়া Amount/Method আসল পেমেন্টের সাথে মিলছে না। সঠিক তথ্য দিয়ে "
            "আবার Deposit চেষ্টা করুন, অথবা Support এ যোগাযোগ করুন।",
        )
    except Exception:
        pass
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                "🚫 <b>Deposit Auto-Rejected — Mismatch</b>\n\n"
                f"🆔 DEP-{dep['id']} (user {dep['user_id']})\n"
                f"🔑 {_id_label(sms['method'])}: <code>{sms['trx_id']}</code> (মিলেছে)\n\n"
                + "\n".join(mismatch_lines),
            )
        except Exception:
            pass


def try_auto_approve_from_stored_sms(dep):
    """একটা নতুন pending deposit তৈরি হওয়ার সাথে সাথেই, আগে থেকেই সেইভ করা কোনো
    ব্যবহার-না-হওয়া SMS এর সাথে TrxID মিলছে কিনা চেক করে; method+amount ও
    মিললে তবেই অটো-অ্যাপ্রুভ করে। রিটার্ন করে: "approved" | "rejected_mismatch" | "no_match" """
    if dep["method"] not in SMS_AUTO_APPROVE_METHODS:
        return "no_match"   # Binance ইত্যাদি -> কখনোই অটো-অ্যাপ্রুভ চেষ্টা করা হয় না

    trx = (dep.get("trx_id") or "").strip().upper()
    if not trx:
        return "no_match"

    sms = next((s for s in sms_log if not s["is_used"] and s["trx_id"].upper() == trx), None)
    if not sms:
        return "no_match"

    amount_ok = _amounts_match(dep["method"], dep.get("amount"), sms["amount"])
    method_ok = _methods_match(dep["method"], sms["method"])
    if amount_ok and method_ok:
        return "approved" if _auto_approve_deposit_with_sms(dep, sms) else "no_match"

    _auto_reject_mismatched_deposit(dep, sms, method_ok, amount_ok)
    return "rejected_mismatch"


def store_sms_and_try_match(text, sender="", hint_method=""):
    """ওয়েবহুক থেকে আসা SMS/নোটিফিকেশন পার্স + সেইভ করে, এবং কোনো pending
    ডিপোজিটের সাথে (TrxID দিয়ে) ম্যাচ করলে সাথে সাথে অটো-অ্যাপ্রুভ করে দেয়।
    ফরোয়ার্ডার অ্যাপ রিট্রাই করলে (একই SMS দুইবার আসলে) ডুপ্লিকেট এন্ট্রি বানায় না।"""
    parsed = parse_payment_sms(text, hint_method=hint_method, sender=sender)
    if not parsed:
        return {"stored": False, "reason": "not_a_payment_sms"}

    for s in sms_log:
        if parsed["method"] == "Binance":
            if s["raw_text"] == text[:1000]:
                return {"stored": True, "sms_id": s["id"], "duplicate": True}
        else:
            if s["trx_id"] == parsed["trx_id"] and s["amount"] is not None and abs(s["amount"] - parsed["amount"]) < 0.01:
                return {"stored": True, "sms_id": s["id"], "duplicate": True}

    sms_id = _next_sms_id()
    sms = {
        "id": sms_id,
        "method": parsed["method"],
        "amount": parsed["amount"],
        "trx_id": parsed["trx_id"],
        "raw_text": text[:1000],
        "sender": (sender or "")[:64],
        "is_used": False,
        "used_by_deposit_id": None,
        "date": datetime.datetime.now().strftime("%d/%m/%Y %I:%M %p"),
    }
    sms_log.append(sms)

    note_line = (
        "🧑‍💻 Binance সবসময় Admin ম্যানুয়ালি Approve করবে (auto-approve হবে না)।"
        if sms["method"] == "Binance"
        else f"⏳ User {_id_label(sms['method'])} + Amount সাবমিট করলে auto-approve হবে।"
    )

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                "📥 <b>New SMS Received</b>\n\n"
                f"💳 Method: {sms['method'] or '—'}\n"
                f"🔑 {_id_label(sms['method'])}: <code>{sms['trx_id']}</code>\n"
                f"💰 Amount: {sms['amount']} {_amount_unit(sms['method'])}\n"
                f"📅 Time: {sms['date']}\n\n"
                f"{note_line}",
            )
        except Exception:
            pass

    # Binance -> কখনোই অটো-ম্যাচ/অটো-অ্যাপ্রুভ চেষ্টা করা হয় না, সবসময় Admin
    # ম্যানুয়ালি রিভিউ করবে (উপরে শুধু রেফারেন্সের জন্য নোটিফিকেশন পাঠানো হলো)।
    matched_dep_id = None
    if sms["method"] != "Binance":
        dep = next(
            (
                d for d in deposits.values()
                if d["status"] == "pending"
                and d.get("trx_id")
                and d["trx_id"].strip().upper() == sms["trx_id"].upper()
            ),
            None,
        )
        if dep:
            amount_ok = _amounts_match(sms["method"], dep.get("amount"), sms["amount"])
            method_ok = _methods_match(dep.get("method"), sms["method"])
            if amount_ok and method_ok:
                if _auto_approve_deposit_with_sms(dep, sms):
                    matched_dep_id = dep["id"]
            else:
                _auto_reject_mismatched_deposit(dep, sms, method_ok, amount_ok)

    return {"stored": True, "sms_id": sms_id, "matched_deposit_id": matched_dep_id}


# ---------------------------------------------------------------------------
# SMS AUTO-DEPOSIT WEBHOOK SERVER (SMS Forwarder App -> এই বট)
# ---------------------------------------------------------------------------
# ফোনে ইনস্টল করা SMS Forwarder App (যেমন "SMS Forwarder", "Sms2Telegram" ইত্যাদি)
# থেকে bKash/Nagad/Rocket/Binance এর পেমেন্ট SMS এখানে ফরোয়ার্ড করা হবে। যেকেউ
# রিকোয়েস্ট পাঠিয়ে ভুয়া ব্যালেন্স যোগ করার চেষ্টা করতে পারে, তাই একটা গোপন
# TOKEN বাধ্যতামূলক — .env / Railway Variables এ SMS_WEBHOOK_TOKEN সেট করুন।
SMS_WEBHOOK_TOKEN = os.environ.get("SMS_WEBHOOK_TOKEN", "").strip()
SMS_WEBHOOK_PATH = os.environ.get("SMS_WEBHOOK_PATH", "/sms-webhook").strip() or "/sms-webhook"
if not SMS_WEBHOOK_TOKEN:
    SMS_WEBHOOK_TOKEN = uuid.uuid4().hex
    print(
        "⚠️  SMS_WEBHOOK_TOKEN সেট করা ছিল না — একটা টেম্পোরারি টোকেন তৈরি করা হয়েছে "
        f"(বট রিস্টার্টে বদলে যাবে): {SMS_WEBHOOK_TOKEN}\n"
        f"   স্থায়ী রাখতে .env / Railway Variables এ যোগ করুন: SMS_WEBHOOK_TOKEN={SMS_WEBHOOK_TOKEN}"
    )

try:
    from flask import Flask, request as flask_request

    flask_app = Flask(__name__)
except ImportError:
    flask_app = None
    print("⚠️  Flask ইনস্টল করা নেই, তাই SMS auto-deposit webhook চালু হবে না। ইনস্টল করুন: pip install flask")

_SMS_TEXT_FIELD_NAMES = ("text", "message", "body", "content", "sms", "msg", "text_message", "sms_body", "smsBody", "key")
_SMS_SENDER_FIELD_NAMES = ("from", "sender", "number", "phone", "sms_from", "smsFrom", "originator")


def _dig_field(d, names):
    """dict এর মধ্যে (nested dict হলেও একটু খুঁজে) common field name গুলো চেক করে।"""
    if not isinstance(d, dict):
        return ""
    for k in names:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in d.values():
        if isinstance(v, dict):
            found = _dig_field(v, names)
            if found:
                return found
    return ""


def _check_sms_token(req):
    token = req.args.get("token") or req.headers.get("X-Webhook-Token") or ""
    return token == SMS_WEBHOOK_TOKEN


if flask_app is not None:

    @flask_app.route(SMS_WEBHOOK_PATH, methods=["GET", "POST"])
    def sms_webhook_handler():
        if not _check_sms_token(flask_request):
            return {"ok": False, "error": "invalid_token"}, 401

        text = flask_request.args.get("text") or flask_request.args.get("message") or ""
        sender = flask_request.args.get("from") or flask_request.args.get("sender") or ""
        hint_method = flask_request.args.get("method") or ""

        if flask_request.method == "POST":
            try:
                raw_body_str = flask_request.get_data(as_text=True) or ""
            except Exception:
                raw_body_str = ""

            body = {}
            if raw_body_str:
                try:
                    parsed_json = json.loads(raw_body_str)
                    if isinstance(parsed_json, dict):
                        body = parsed_json
                except Exception:
                    if "=" in raw_body_str and "&" in raw_body_str:
                        try:
                            from urllib.parse import parse_qs

                            parsed_form = parse_qs(raw_body_str)
                            body = {k: v[0] for k, v in parsed_form.items() if v}
                        except Exception:
                            body = {}

            if body:
                text = text or _dig_field(body, _SMS_TEXT_FIELD_NAMES)
                sender = sender or _dig_field(body, _SMS_SENDER_FIELD_NAMES)
                hint_method = hint_method or _dig_field(body, ("method", "app", "provider"))

            if not text and raw_body_str and not raw_body_str.lstrip().startswith(("{", "[")):
                text = raw_body_str

        if not text:
            # ✅ SMS Forwarder app যেন সবসময় HTTP 200 পায় (নাহলে app এটাকে Fail/Retry ধরে)
            return {"ok": True, "note": "no_text_field"}, 200

        try:
            result = store_sms_and_try_match(text, sender=sender, hint_method=hint_method)
        except Exception as e:
            print(f"❌ sms_webhook_handler error: {e}")
            return {"ok": True, "note": "internal_error_logged"}, 200

        return {"ok": True, **result}, 200

    @flask_app.route("/", methods=["GET"])
    def _sms_webhook_health():
        return {"ok": True, "service": "sms-webhook", "path": SMS_WEBHOOK_PATH}, 200


def _print_sms_webhook_url():
    if RAILWAY_URL:
        full_url = f"{RAILWAY_URL}{SMS_WEBHOOK_PATH}?token={SMS_WEBHOOK_TOKEN}"
    else:
        full_url = f"http://<your-server-ip>:{PORT}{SMS_WEBHOOK_PATH}?token={SMS_WEBHOOK_TOKEN}"
    print(
        f"📩 SMS webhook ready → {full_url}\n"
        "   ফরোয়ার্ডার অ্যাপে GET/POST params হিসেবে পাঠান: text (SMS বডি), from (sender, ঐচ্ছিক)"
    )


# ---------------------------------------------------------------------------
# RUN (Polling for local test / Webhook for Railway)
# ---------------------------------------------------------------------------
def run_polling():
    start_persistent_db()   # ✅ ডিস্কে সেভ করা DB লোড + অটোসেভ থ্রেড চালু
    print("Bot running in POLLING mode...")
    if flask_app is not None:
        # SMS webhook এর জন্য আলাদা থ্রেডে ছোট একটা Flask সার্ভার চালু হচ্ছে,
        # যাতে polling মোডেও SMS Forwarder App থেকে অটো-ডিপোজিট কাজ করে।
        threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
            daemon=True,
        ).start()
        _print_sms_webhook_url()
    bot.remove_webhook()
    bot.infinity_polling()


def run_webhook():
    start_persistent_db()   # ✅ ডিস্কে সেভ করা DB লোড + অটোসেভ থ্রেড চালু
    if flask_app is None:
        print("❌ Flask ইনস্টল করা নেই, তাই WEBHOOK mode চালু করা যাচ্ছে না। ইনস্টল করুন: pip install flask")
        return

    app = flask_app
    webhook_path = f"/webhook/{BOT_TOKEN}"

    @app.route(webhook_path, methods=["POST"])
    def telegram_webhook():
        json_str = flask_request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200

    bot.remove_webhook()
    bot.set_webhook(url=f"{RAILWAY_URL}{webhook_path}")
    print(f"Bot running in WEBHOOK mode on port {PORT} -> {RAILWAY_URL}{webhook_path}")
    _print_sms_webhook_url()
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    if RAILWAY_URL:
        run_webhook()
    else:
        run_polling()
