import asyncio
import sqlite3
import zipfile
import tempfile
import shutil
import json
import html
import os
import io
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta

import qrcode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
    InputMediaVideo,
)
from telegram.error import TelegramError, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters,
)

BOT_TOKEN = "9260427892:AAG1y52MdyFWfMbRTwWGecwtZ6DW2dVVX_s"
ADMIN_ID = 8853901359
ADMIN_IDS = {ADMIN_ID}
try:
    ADMIN_IDS.update(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
except ValueError:
    pass
DB_FILE = "nhext.sqlite"
EMOJI_FILE = "multi.txt"
ENABLE_PREMIUM_EMOJI = True
LIFETIME_DAYS = 36500
DEFAULT_WELCOME = "💎 WELCOME TO PREMIUM 💎\n\n👇 Choose a plan below"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# =========================================================
# LOAD EMOJI IDS FROM multi.txt
# Format: each line contains "<emoji>  <custom_emoji_id>"
# =========================================================
def _load_emoji_map_from_file(path):
    m = {}
    if not os.path.exists(path):
        logger.warning(f"Emoji file not found: {path}")
        return m
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Skip separator / header lines
                if line.startswith("=") or line.startswith("PACK:") or line.startswith("TITLE:") or line.startswith("COUNT:"):
                    continue
                # Remove leading index number: "1. ✅  6129812419028982717"
                if ". " in line[:6]:
                    parts = line.split(".", 1)
                    if len(parts) == 2 and parts[0].strip().isdigit():
                        line = parts[1].strip()
                # Now line = "✅  6129812419028982717"  (emoji then whitespace then id)
                bits = line.rsplit(None, 1)
                if len(bits) != 2:
                    continue
                emoji_part, id_part = bits[0].strip(), bits[1].strip()
                if not id_part.isdigit():
                    continue
                if not emoji_part:
                    continue
                # Keep FIRST occurrence of each emoji (avoid overwriting)
                if emoji_part not in m:
                    m[emoji_part] = id_part
    except Exception as e:
        logger.exception(f"Failed to load emoji file: {e}")
    return m


PREMIUM_EMOJI_IDS = _load_emoji_map_from_file(EMOJI_FILE)
_EMOJI_SORTED = sorted(PREMIUM_EMOJI_IDS.keys(), key=len, reverse=True)
logger.info(f"Loaded {len(PREMIUM_EMOJI_IDS)} premium emojis from {EMOJI_FILE}")


def build_entities(text):
    if not text:
        return "", []
        
    import re
    # Extract <code>, <s>, <b> tags
    pattern = re.compile(r'<(code|s|b)>(.*?)</\1>', re.DOTALL)
    
    out = []
    entities_data = []
    last_end = 0
    clean_len = 0
    
    for match in pattern.finditer(text):
        normal_text = text[last_end:match.start()]
        out.append(normal_text)
        clean_len += len(normal_text)
        
        tag_type = match.group(1)
        inner_text = match.group(2)
        start_clean = clean_len
        out.append(inner_text)
        clean_len += len(inner_text)
        
        entities_data.append((tag_type, start_clean, start_clean + len(inner_text)))
        last_end = match.end()
        
    out.append(text[last_end:])
    clean = "".join(out)
    
    u16 = []
    total = 0
    for ch in clean:
        u16.append(total)
        total += 2 if ord(ch) > 0xFFFF else 1
    u16.append(total)
    
    entities = []
    if clean:
        entities.append(MessageEntity(type=MessageEntity.BOLD, offset=0, length=u16[-1]))
        
    for tag_type, s, e in entities_data:
        if s < len(u16) and e < len(u16):
            length = u16[e] - u16[s]
            if tag_type == "code":
                entities.append(MessageEntity(type=MessageEntity.CODE, offset=u16[s], length=length))
            elif tag_type == "s":
                entities.append(MessageEntity(type=MessageEntity.STRIKETHROUGH, offset=u16[s], length=length))
            elif tag_type == "b":
                entities.append(MessageEntity(type=MessageEntity.BOLD, offset=u16[s], length=length))
                
    if ENABLE_PREMIUM_EMOJI and _EMOJI_SORTED:
        used = set()
        i = 0
        n = len(clean)
        while i < n:
            hit = None
            for k in _EMOJI_SORTED:
                if clean.startswith(k, i):
                    hit = k
                    break
            if hit:
                cid = PREMIUM_EMOJI_IDS[hit]
                if cid not in used and i + len(hit) <= n:
                    used.add(cid)
                    entities.append(MessageEntity(
                        type=MessageEntity.CUSTOM_EMOJI,
                        offset=u16[i],
                        length=u16[i + len(hit)] - u16[i],
                        custom_emoji_id=cid,
                    ))
                i += len(hit)
            else:
                i += 1
                
    entities.sort(key=lambda e: e.offset)
    return clean, entities


async def send_msg(bot, chat_id, text, reply_markup=None):
    clean, ents = build_entities(text)
    protect = get_setting("protect_content", "0") == "1"
    try:
        return await bot.send_message(chat_id, clean, entities=ents, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e1:
        logger.warning(f"send_msg entities failed: {e1}")
    try:
        return await bot.send_message(chat_id, clean, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e2:
        logger.warning(f"send_msg plain failed: {e2}")
    plain = "".join(c for c in clean if ord(c) < 0x1F000)
    return await bot.send_message(chat_id, plain, reply_markup=reply_markup, protect_content=protect)


async def reply_msg(update, text, reply_markup=None):
    if not update.message:
        return None
    clean, ents = build_entities(text)
    protect = get_setting("protect_content", "0") == "1"
    try:
        return await update.message.reply_text(clean, entities=ents, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e1:
        logger.warning(f"reply_msg entities failed: {e1}")
    try:
        return await update.message.reply_text(clean, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e2:
        logger.warning(f"reply_msg plain failed: {e2}")
    plain = "".join(c for c in clean if ord(c) < 0x1F000)
    return await update.message.reply_text(plain, reply_markup=reply_markup, protect_content=protect)


async def reply_q(q, text, reply_markup=None):
    clean, ents = build_entities(text)
    protect = get_setting("protect_content", "0") == "1"
    try:
        return await q.message.reply_text(clean, entities=ents, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e1:
        logger.warning(f"reply_q entities failed: {e1}")
    try:
        return await q.message.reply_text(clean, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e2:
        logger.warning(f"reply_q plain failed: {e2}")
    plain = "".join(c for c in clean if ord(c) < 0x1F000)
    return await q.message.reply_text(plain, reply_markup=reply_markup, protect_content=protect)


async def edit_msg(q, text, reply_markup=None):
    clean, ents = build_entities(text)
    try:
        return await q.edit_message_text(clean, entities=ents, reply_markup=reply_markup)
    except TelegramError as e1:
        logger.warning(f"edit_msg entities failed: {e1}")
    try:
        return await q.edit_message_text(clean, reply_markup=reply_markup)
    except TelegramError as e2:
        logger.warning(f"edit_msg plain failed: {e2}")
    plain = "".join(c for c in clean if ord(c) < 0x1F000)
    try:
        return await q.edit_message_text(plain, reply_markup=reply_markup)
    except TelegramError:
        return await reply_q(q, text, reply_markup)


async def send_html(bot, chat_id, text, reply_markup=None):
    """Send message with HTML parse mode — supports <s>, <b>, <code>, <a>."""
    protect = get_setting("protect_content", "0") == "1"
    try:
        return await bot.send_message(
            chat_id, text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            protect_content=protect,
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.warning(f"send_html failed: {e}")
        return None


async def send_photo_msg(bot, chat_id, photo, text, reply_markup=None, force_protect=None):
    clean, ents = build_entities(text)
    if force_protect is None:
        protect = get_setting("protect_content", "0") == "1"
    else:
        protect = force_protect
    try:
        return await bot.send_photo(chat_id, photo, caption=clean, caption_entities=ents, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e1:
        logger.warning(f"send_photo_msg entities failed: {e1}")
    try:
        return await bot.send_photo(chat_id, photo, caption=clean, reply_markup=reply_markup, protect_content=protect)
    except TelegramError as e2:
        logger.warning(f"send_photo_msg plain failed: {e2}")
    plain = "".join(c for c in clean if ord(c) < 0x1F000)
    return await bot.send_photo(chat_id, photo, caption=plain, reply_markup=reply_markup, protect_content=protect)


def db():
    con = sqlite3.connect(DB_FILE, timeout=60, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA cache_size=-64000")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(v):
    return html.escape("" if v is None else str(v))


def strike(text):
    """Unicode combining strikethrough — works everywhere, no entities needed."""
    return "".join(ch + "\u0336" for ch in str(text))


WELCOME_TEXT_DEFAULT = (
    "🔥 Welcome to CORN 🌽 STORE! 🔥\n\n👋 Hello KHARIDYE CHOOT KE VIDEOS 😘🤡\n\n🫦𝐏𝐑𝐄𝐌𝐈𝐔𝐌 𝐕𝐈𝐑𝐀𝐋 𝐂𝐎𝐋𝐋𝐄𝐂𝐓𝐈𝐎𝐍 𝐔𝐍𝐋𝐎𝐂𝐊𝐄𝐃🔥\n\n🤯 ALL IN ONE GROUP 💋💦 \n🌚 Mom & Kid 🍼😍 -\n🥵 CH!LD  P@RN💀 - \n🧿 Viral LE@KS 🍼🛒 \n🌝R@pe  🛒🤐 -\n👄 Indian desi 💦 - \n\n🚸INSTANT JOIN \n💯DIRECT VIDEOS AND TRUSTED 🥵\n\n⬇️ Choose a plan below:"
)


def init_db():
    con = db()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            registered_at TEXT NOT NULL, premium_until TEXT, status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL,
            validity_days INTEGER NOT NULL, premium_link TEXT, plan_message TEXT,
            pay_message TEXT, image_file_id TEXT, enabled INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS demo_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, file_id TEXT, sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT,
            plan_id INTEGER NOT NULL, plan_name TEXT, amount REAL, validity INTEGER,
            screenshot_file_id TEXT, utr TEXT, status TEXT NOT NULL DEFAULT 'pending',
            rejection_reason TEXT, created_at TEXT NOT NULL, approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, content_type TEXT, file_id TEXT, text TEXT,
            target TEXT, created_at TEXT, total INTEGER, success INTEGER, failed INTEGER
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, details TEXT, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
        CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
        CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
        """)
        defaults = {
            "welcome_text": WELCOME_TEXT_DEFAULT,
            "welcome_image": "",
            "welcome_videos": "[\"BAACAgUAAxkBAAMWatxbyRKTDYhd0aBu4BT04LTg0RkAAhMgAAL1BUBWeJTdds-cryM9BA\", \"BAACAgUAAxkBAAMXatxbyTKEBG1xeBaBEw5wuu6ALW8AAhQgAAL1BUBW-ZBK2G_mOmY9BA\", \"BAACAgUAAxkBAAMYatxbyUhJT6I71OgSpbzdd2WGUc8AAhUgAAL1BUBWbDGKrUBPnNk9BA\", \"BAACAgUAAxkBAAMZatxbyf0urbnUwLK4ukXp2Ae6lJ0AAhYgAAL1BUBWBRKj6cU_diY9BA\"]",
            "extra_admins": "",
            "upi_id": "Q026188928@ybl",
            "support_id": "",
            "howtobuy_msg": "📖 How to Buy\n\n1. Pick a plan\n2. Pay via UPI\n3. Send screenshot + UTR\n4. Instant access",
            "proof_channel": "https://t.me/MMSBHAIXPRFm",
            "premium_channel": "",
            "plan_photo": "",
            "protect_content": "1",
        }
        for k, v in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        # Always update welcome_text if it's still the default placeholder
        cur = con.execute("SELECT value FROM settings WHERE key='welcome_text'").fetchone()
        if cur and cur["value"] == DEFAULT_WELCOME:
            con.execute("UPDATE settings SET value=? WHERE key='welcome_text'", (WELCOME_TEXT_DEFAULT,))
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "premium_until" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
        if "status" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
        pcols = {r["name"] for r in con.execute("PRAGMA table_info(plans)").fetchall()}
        for c in ("premium_link", "plan_message", "pay_message", "image_file_id"):
            if c not in pcols:
                con.execute(f"ALTER TABLE plans ADD COLUMN {c} TEXT")
        if "enabled" not in pcols:
            con.execute("ALTER TABLE plans ADD COLUMN enabled INTEGER DEFAULT 1")
        # demo_media me plan_id column
        dcols = {r["name"] for r in con.execute("PRAGMA table_info(demo_media)").fetchall()}
        if "plan_id" not in dcols:
            con.execute("ALTER TABLE demo_media ADD COLUMN plan_id INTEGER DEFAULT NULL")
        paycols = {r["name"] for r in con.execute("PRAGMA table_info(payments)").fetchall()}
        for c in ("rejection_reason", "approved_at"):
            if c not in paycols:
                con.execute(f"ALTER TABLE payments ADD COLUMN {c} TEXT")
        # ── SEED DEFAULT PLANS (only if empty) ──
        _count = con.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"]
        if _count == 0:
            _seed_plans = [
                ("Mo0m&son🫦 — ₹40 🔥 75% OFF", 40.0, 36500, "https://t.me/+JAf5ASvtLrliYzhl", "━━━━━━━━━━━━━━━\n🔥 75% OFF\n🏷 MRP: <s>₹159</s> → ₹40\n💸 You Save: ₹119 (75% OFF)\n━━━━━━━━━━━━━━━\n\n✨ Mo0m&son🫦 ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹40 (<s>₹159</s> 75% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: Mo0m&son🫦\n💰 Amount: <s>₹40</s> ₹40\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹40 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("Child Pack💎 — ₹50 🔥 90% OFF", 50.0, 36500, "https://t.me/+06Vq-5EdTQMzYjc1", "━━━━━━━━━━━━━━━\n🔥 90% OFF\n🏷 MRP: <s>₹499</s> → ₹50\n💸 You Save: ₹449 (90% OFF)\n━━━━━━━━━━━━━━━\n\n✨ Child Pack💎 ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹50 (<s>₹499</s> 90% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: Child Pack💎\n💰 Amount: <s>₹50</s> ₹50\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹50 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("INDIAN DESI🥵 — ₹45", 45.0, 36500, "https://t.me/+7MOzaD6XpuVkYTk1", "🏷 MRP: <s>₹135</s> → ₹45\n━━━━━━━━━━━━━━━\n💰 Price: ₹45\n━━━━━━━━━━━━━━━\n\n✨ INDIAN DESI🥵 ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹45\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: INDIAN DESI🥵\n💰 Amount: <s>₹45</s> ₹45\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹45 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("R4P€ INDIAN — ₹40 🔥 60% OFF", 40.0, 36500, "https://t.me/+ZM5_x0963I5kN2Fl", "━━━━━━━━━━━━━━━\n🔥 60% OFF\n🏷 MRP: <s>₹100</s> → ₹40\n💸 You Save: ₹60 (60% OFF)\n━━━━━━━━━━━━━━━\n\n✨ R4P€ INDIAN ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹40 (<s>₹100</s> 60% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: R4P€ INDIAN\n💰 Amount: <s>₹40</s> ₹40\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹40 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("BHA! B€HEN/bro& sis — ₹60 🔥 60% OFF", 60.0, 36500, "https://t.me/+So1J-Db3n55kMzM1", "━━━━━━━━━━━━━━━\n🔥 60% OFF\n🏷 MRP: <s>₹150</s> → ₹60\n💸 You Save: ₹90 (60% OFF)\n━━━━━━━━━━━━━━━\n\n✨ BHA! B€HEN/bro& sis ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹60 (<s>₹150</s> 60% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: BHA! B€HEN/bro& sis\n💰 Amount: <s>₹60</s> ₹60\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹60 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("👄New Ch!|d p@ck — ₹149 🔥 70% OFF", 149.0, 36500, "https://t.me/+xIpfXZ8sFi02Zjhl", "━━━━━━━━━━━━━━━\n🔥 70% OFF\n🏷 MRP: <s>₹498</s> → ₹149\n💸 You Save: ₹349 (70% OFF)\n━━━━━━━━━━━━━━━\n\n✨ 🫦New Ch!|d p@ck ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹149 (<s>₹498</s> 70% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: 🫦New Ch!|d p@ck\n💰 Amount: <s>₹149</s> ₹149\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹149 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("All in one 👄 — ₹200 🔥 80% OFF", 200.0, 36500, "https://t.me/+JAf5ASvtLrliYzhl", "━━━━━━━━━━━━━━━\n🔥 80% OFF\n🏷 MRP: <s>₹1000</s> → ₹200\n💸 You Save: ₹800 (80% OFF)\n━━━━━━━━━━━━━━━\n\n✨ All in one 👄 ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹200 (<s>₹1000</s> 80% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: All in one 👄\n💰 Amount: <s>₹200</s> ₹200\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹200 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
                ("🌚Special Lifetime 🥵 — ₹750 🔥 25% OFF", 750.0, 36500, "https://t.me/+06Vq-5EdTQMzYjc1", "━━━━━━━━━━━━━━━\n🔥 25% OFF\n🏷 MRP: <s>₹1000</s> → ₹750\n💸 You Save: ₹250 (25% OFF)\n━━━━━━━━━━━━━━━\n\n✨ 🌚Special Lifetime 🥵 ✨\n━━━━━━━━━━━━━━━\n📌 All new direct\n💰 Price: ₹750 (<s>₹1000</s> 25% OFF)\n━━━━━━━━━━━━━━━\n\n⬇️ Tap below to pay & get your ID instantly!", "💳 Scan & Pay Securely ✨\n━━━━━━━━━━━━━━━\n🛍 Plan: 🌚Special Lifetime 🥵\n💰 Amount: <s>₹750</s> ₹750\n💳 UPI: {upi}\n━━━━━━━━━━━━━━━\n\n📱 Scan this QR with any UPI app\n✔️ Amount ₹750 fills automatically\n📱 After paying, tap Verify Payment & send screenshot", 1),
            ]
            for _row in _seed_plans:
                con.execute(
                    "INSERT INTO plans(name, price, validity_days, premium_link, plan_message, pay_message, enabled) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    _row,
                )
            logger.info(f"Seeded {len(_seed_plans)} default plans from DB snapshot")
    finally:
        con.close()


def get_setting(key, default=None):
    con = db()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if r is None or r["value"] is None:
            return default
        return r["value"]
    finally:
        con.close()


def set_setting(key, value):
    con = db()
    try:
        con.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    finally:
        con.close()


def log_event(event, details=""):
    try:
        con = db()
        try:
            con.execute("INSERT INTO logs(event, details, created_at) VALUES(?, ?, ?)", (event, details, now_iso()))
        finally:
            con.close()
    except Exception as e:
        logger.warning(f"log_event failed: {e}")


def register_user(user):
    con = db()
    try:
        con.execute(
            "INSERT INTO users(id, username, first_name, last_name, registered_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name",
            (user.id, user.username or "", user.first_name or "", user.last_name or "", now_iso()),
        )
    finally:
        con.close()


def current_admin_ids():
    ids = set(ADMIN_IDS)
    extra = get_setting("extra_admins", "") or ""
    for x in extra.split(","):
        x = x.strip()
        if x:
            try:
                ids.add(int(x))
            except ValueError:
                continue
    return ids


def admin_only(obj):
    user = None
    if hasattr(obj, "effective_user") and obj.effective_user is not None:
        user = obj.effective_user
    elif hasattr(obj, "from_user") and obj.from_user is not None:
        user = obj.from_user
    if user is None:
        return False
    return user.id in current_admin_ids()


def safe_name(user):
    if user.username:
        return "@" + user.username
    name = (user.first_name or "") + ((" " + user.last_name) if user.last_name else "")
    return name.strip() or str(user.id)


async def answer(q, text=None, alert=False):
    try:
        if text:
            await q.answer(text, show_alert=alert)
        else:
            await q.answer()
    except TelegramError:
        return


def btn(text, callback_data=None, url=None, style=None):
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style not in {"primary", "success", "danger"}:
        return InlineKeyboardButton(**kwargs)
    try:
        return InlineKeyboardButton(**kwargs, style=style)
    except TypeError:
        try:
            return InlineKeyboardButton(**kwargs, api_kwargs={"style": style})
        except TypeError:
            return InlineKeyboardButton(**kwargs)


def back_kb(target="admin:menu"):
    return InlineKeyboardMarkup([[btn("🔙 Back", target, style="primary")]])


def start_menu_keyboard(plans):
    rows = []
    for p in plans:
        rows.append([btn(f"💎 {p['name']}", f"plan:view:{p['id']}", style="danger")])
    rows.append([btn("📖 How to Buy", "menu:howtobuy", style="success"), btn("🆘 Support", "menu:support", style="danger")])
    rows.append([btn("🎯 Proof of Delivery", "menu:proof", style="success")])
    return InlineKeyboardMarkup(rows)


def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [btn("📊 Dashboard", "admin:dashboard", style="success"), btn("💎 Plans", "admin:plans", style="success")],
        [btn("👥 Users", "admin:users", style="primary"), btn("💳 Payments", "admin:payments", style="primary")],
        [btn("⚙️ Bot Settings", "admin:settings", style="primary")],
        [btn("🖼️ Welcome Settings", "admin:welcome", style="primary")],
        [btn("🥵 Demo Videos", "admin:demo", style="danger")],
        [btn("📢 Broadcast", "admin:broadcast", style="primary")],
        [btn("🛡 Content Protection", "admin:protect", style="primary")],
        [btn("👑 Admin Management", "admin:security", style="primary")],
        [btn("🏷 Change Bot Name", "admin:change_name", style="primary")],
        [btn("🖼 Change Bot Photo", "admin:change_photo", style="primary")],
        [btn("🧬 Clone Bot", "admin:clone_bot", style="danger")],
        [btn("📤 Export DB+Media", "admin:db:export", style="success"), btn("📥 Import DB+Media", "admin:db:import", style="danger")],
        [btn("💾 Backup SQLite", "admin:db:backup", style="primary")],
    ])


def fetch_enabled_plans():
    con = db()
    try:
        return con.execute("SELECT * FROM plans WHERE enabled=1 ORDER BY id ASC").fetchall()
    finally:
        con.close()


def fetch_all_plans():
    con = db()
    try:
        return con.execute("SELECT * FROM plans ORDER BY id ASC").fetchall()
    finally:
        con.close()


def fetch_plan(pid):
    con = db()
    try:
        return con.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    finally:
        con.close()


def fetch_payment(pid):
    con = db()
    try:
        return con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    finally:
        con.close()


def is_user_blocked(uid):
    con = db()
    try:
        r = con.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
        if r is None:
            return False
        return (r["status"] or "active") == "blocked"
    finally:
        con.close()



async def _delete_welcome_media(bot, chat_id):
    """Fast delete welcome media on any button click."""
    try:
        prev = get_setting(f"last_welcome_{chat_id}", "") or ""
        if not prev:
            return
        set_setting(f"last_welcome_{chat_id}", "")
        # Delete in parallel for speed
        tasks = []
        for mid in prev.split(","):
            if mid.strip():
                tasks.append(bot.delete_message(chat_id, int(mid.strip())))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.warning(f"delete welcome failed: {e}")


async def send_start_screen(bot, chat_id):
    welcome_image = get_setting("welcome_image", "") or ""
    welcome_videos_raw = get_setting("welcome_videos", "") or ""
    welcome_text = get_setting("welcome_text", WELCOME_TEXT_DEFAULT) or WELCOME_TEXT_DEFAULT
    plans = fetch_enabled_plans()
    # Delete previous welcome messages (image/video) if any
    await _delete_welcome_media(bot, chat_id)
    sent_ids = []
    if welcome_image:
        try:
            msg = await bot.send_photo(chat_id, welcome_image, protect_content=True, has_spoiler=True)
            sent_ids.append(str(msg.message_id))
        except TelegramError as e:
            logger.warning(f"welcome_image send failed: {e}")
    if welcome_videos_raw:
        try:
            vids = json.loads(welcome_videos_raw)
        except Exception:
            vids = []
        vids = [v for v in vids if v]
        if vids:
            media = [InputMediaVideo(v, has_spoiler=True) for v in vids[:10]]
            try:
                msgs = await bot.send_media_group(chat_id, media=media, protect_content=True)
                for m in msgs:
                    sent_ids.append(str(m.message_id))
            except TelegramError as e:
                logger.warning(f"welcome_videos send failed: {e}")
    kb = start_menu_keyboard(plans)
    main_msg = await send_msg(bot, chat_id, welcome_text, reply_markup=kb)
    try:
        if sent_ids and main_msg:
            set_setting(f"last_welcome_{chat_id}", ",".join(sent_ids))
    except Exception as e:
        logger.warning(f"save welcome ids failed: {e}")


async def _notify_admin_new_user(bot, user):
    txt = (
        "🆕 NEW USER STARTED BOT\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: {esc(user.first_name or '')} {esc(user.last_name or '')}\n"
        f"🔗 Username: @{esc(user.username or 'none')}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    for admin_id in current_admin_ids():
        try:
            await send_msg(bot, admin_id, txt)
        except TelegramError as e:
            logger.warning(f"new user notify {admin_id} failed: {e}")


async def _broadcast_new_purchase(bot, plan_name, amount, payment_id, screenshot_file_id):
    # Caption text
    caption = (
        "👎 NEW PURCHASE SUCCESS! 👎\n"
        "\n"
        f"Someone just bought <s>{plan_name}</s> and got instant access! ⚡\n"
        "\n"
        "<b>Buy Now to get your access:</b>"
    )
    
    # Plans fetch karo keyboard ke liye
    plans = fetch_enabled_plans()
    kb = start_menu_keyboard(plans)
    
    proof = get_setting("proof_channel", "") or ""
    if proof:
        pc = proof
        if proof.startswith("https://t.me/"):
            pc = "@" + proof.rstrip("/").split("/")[-1]
        elif proof.startswith("t.me/"):
            pc = "@" + proof.rstrip("/").split("/")[-1]
        try:
            if screenshot_file_id:
                await send_photo_msg(bot, pc, screenshot_file_id, caption, reply_markup=kb)
            else:
                await send_msg(bot, pc, caption, reply_markup=kb)
        except TelegramError as e:
            logger.warning(f"proof send failed: {e}")
            
    con = db()
    try:
        rows = con.execute("SELECT id FROM users WHERE status='active'").fetchall()
    finally:
        con.close()
    
    sem = asyncio.Semaphore(20)
    async def one(uid):
        async with sem:
            for _ in range(3):
                try:
                    if screenshot_file_id:
                        await send_photo_msg(bot, uid, screenshot_file_id, caption, reply_markup=kb)
                    else:
                        await send_msg(bot, uid, caption, reply_markup=kb)
                    return
                except RetryAfter as e:
                    await asyncio.sleep(float(e.retry_after) + 0.5)
                except TelegramError:
                    return
    await asyncio.gather(*(one(r["id"]) for r in rows))


async def send_plan_demo_batch(bot, chat_id, pid, context):
    from telegram import InputMediaVideo, InputMediaPhoto, InputMediaAnimation
    con = db()
    try:
        rows = con.execute(
            "SELECT media_type, file_id FROM demo_media WHERE plan_id IS NULL OR plan_id=? ORDER BY sort_order ASC, id ASC",
            (pid,),
        ).fetchall()
    finally:
        con.close()

    protect = get_setting("protect_content", "0") == "1"

    # Separate videos/photos — Telegram media_group must be homogeneous-ish
    # Mix rule: photos + videos together allowed only if all have same type OR
    # we send two separate groups (one photos, one videos)
    photos = []
    videos = []
    others = []

    for r in rows:
        mt, fid = r["media_type"], r["file_id"]
        if not fid:
            continue
        if mt == "video":
            videos.append(fid)
        elif mt == "photo":
            photos.append(fid)
        elif mt == "animation":
            others.append(("animation", fid))
        else:
            others.append(("doc", fid))

    sent_msgs = []

    # ── Group 1: PHOTOS (up to 10) ──
    if photos:
        try:
            media = [InputMediaPhoto(fid, has_spoiler=True) for fid in photos[:10]]
            msgs = await bot.send_media_group(chat_id, media=media, protect_content=protect)
            sent_msgs.extend(msgs)
        except TelegramError as e:
            logger.warning(f"photo demo batch failed: {e}")

    # ── Group 2: VIDEOS (up to 10) ──
    if videos:
        try:
            media = [InputMediaVideo(fid, has_spoiler=True) for fid in videos[:10]]
            msgs = await bot.send_media_group(chat_id, media=media, protect_content=protect)
            sent_msgs.extend(msgs)
        except TelegramError as e:
            logger.warning(f"video demo batch failed: {e}")

    # ── Other media (animation/doc) sent individually ──
    for kind, fid in others[:5]:
        try:
            if kind == "animation":
                msg = await bot.send_animation(chat_id, fid, has_spoiler=True, protect_content=protect)
            else:
                msg = await bot.send_document(chat_id, fid, protect_content=protect)
            sent_msgs.append(msg)
        except TelegramError as e:
            logger.warning(f"{kind} demo failed: {e}")

    plan = fetch_plan(pid)
    if plan is None:
        return
    price = float(plan["price"])
    name = plan["name"]
    default_msg = (
        f"💎 {name}\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: ₹{price:g}\n⏳ Validity: Lifetime\n"
        f"🔒 Secure UPI payment\n━━━━━━━━━━━━━━━━━━━━\n🎯 Tap Pay Now"
    )
    msg = plan["plan_message"] or default_msg
    kb = InlineKeyboardMarkup([
        [btn("💳 Pay Now (Secure UPI)", f"plan:pay:{pid}", style="success")],
        [btn("🔙 Back to Plans", "menu:home", style="primary")],
    ])
    try:
        # Try HTML send first (supports <s> strikethrough properly)
        main = await send_html(bot, chat_id, msg, reply_markup=kb)
        if main is None:
            # Fallback: plain send
            main = await send_msg(bot, chat_id, msg, reply_markup=kb)
        if main:
            sent_msgs.append(main)
    except TelegramError as e:
        logger.warning(f"plan detail send failed: {e}")

    context.user_data["plan_demo_msgs"] = [str(m.message_id) for m in sent_msgs if m]


async def delete_plan_demo_msgs(bot, chat_id, context):
    ids = context.user_data.pop("plan_demo_msgs", None) or []
    if not ids:
        return
    tasks = []
    for mid in ids:
        try:
            tasks.append(bot.delete_message(chat_id, int(mid)))
        except (ValueError, TypeError):
            continue
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def menu_home(q, context):
    await answer(q)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    await send_start_screen(context.bot, q.message.chat_id)


async def menu_howtobuy(q, context):
    await answer(q)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    msg = get_setting("howtobuy_msg", "") or "📖 How to Buy"
    kb = InlineKeyboardMarkup([[btn("🔙 Back", "menu:home", style="primary")]])
    await send_msg(context.bot, q.message.chat_id, msg, reply_markup=kb)


async def menu_support(q, context):
    await answer(q)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    msg = "🆘 Need Help?\n━━━━━━━━━━━━━━━━━━━━\nOur support team is ready to help.\nTap below to reach out."
    kb = InlineKeyboardMarkup([
        [btn("💬 Contact Support", "menu:support:contact", style="success")],
        [btn("🔙 Cancel", "menu:home", style="danger")],
    ])
    await send_msg(context.bot, q.message.chat_id, msg, reply_markup=kb)


async def menu_support_contact(q, context):
    support = get_setting("support_id", "") or ""
    if not support:
        await answer(q, "⚠️ Support not configured", True)
        return
    await answer(q)
    if support.startswith("http") or support.startswith("tg://"):
        url = support
    elif support.startswith("@"):
        url = f"https://t.me/{support.lstrip('@')}"
    else:
        url = f"https://t.me/{support.lstrip('@')}"
    msg = f"💬 Contact Support\n━━━━━━━━━━━━━━━━━━━━\n👤 Support: {support}\nTap below to open chat."
    kb = InlineKeyboardMarkup([
        [btn("💬 Open Support", url=url, style="success")],
        [btn("🔙 Back", "menu:support", style="primary")],
    ])
    await edit_msg(q, msg, reply_markup=kb)


async def menu_proof(q, context):
    await answer(q)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    proof = get_setting("proof_channel", "") or ""
    rows = []
    if proof:
        if proof.startswith("http") or proof.startswith("tg://"):
            url = proof
        else:
            url = f"https://t.me/{proof.lstrip('@')}"
        rows.append([btn("🏪 Join Our Channel", url=url, style="success")])
    rows.append([btn("🔙 Back", "menu:home", style="primary")])
    msg = "🎯 Proof of Our Delivery\n━━━━━━━━━━━━━━━━━━━━\nAll deliveries verified.\nJoin our channel for live proof & updates."
    await send_msg(context.bot, q.message.chat_id, msg, reply_markup=InlineKeyboardMarkup(rows))


async def plan_view(q, context, pid):
    await answer(q)
    plan = fetch_plan(pid)
    if plan is None or not plan["enabled"]:
        await reply_q(q, "❌ Plan not available")
        return
    await delete_plan_demo_msgs(context.bot, q.message.chat_id, context)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    await send_plan_demo_batch(context.bot, q.message.chat_id, pid, context)


async def plan_pay(q, context, pid):
    await answer(q)
    plan = fetch_plan(pid)
    if plan is None or not plan["enabled"]:
        await reply_q(q, "❌ Plan not available")
        return
    upi = get_setting("upi_id", "") or ""
    if not upi:
        try:
            await edit_msg(q, "⚠️ UPI not configured")
        except TelegramError:
            await reply_q(q, "⚠️ UPI not configured")
        return
    await delete_plan_demo_msgs(context.bot, q.message.chat_id, context)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    price = float(plan["price"])
    amount = f"{price:.2f}".rstrip("0").rstrip(".")
    uri = "upi://pay?" + urllib.parse.urlencode({"pa": upi, "pn": "Premium", "am": amount, "cu": "INR"})
    qr = io.BytesIO()
    try:
        qrcode.make(uri).save(qr, format="PNG")
    except Exception as e:
        logger.exception(f"QR gen failed: {e}")
        await send_msg(context.bot, q.message.chat_id, "❌ Could not generate QR. Contact support.")
        return
    qr.seek(0)
    name = plan["name"]
    default_msg = (
        "💳 Complete Payment\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 Plan: {name}\n"
        f"💰 Amount: ₹{price:g}\n"
        f"🏦 UPI: <code>{esc(upi)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📸 Scan QR & pay\n"
        "🔒 Then tap Verify Payment"
    )
    msg = plan["pay_message"] or default_msg
    msg = msg.replace("{upi}", upi)
    kb = InlineKeyboardMarkup([
        [btn("✅ Verify Payment", f"plan:verify:{pid}", style="success")],
        [btn("🔙 Back to Plans", "menu:home", style="primary")],
    ])
    # Try HTML send as caption (photo with HTML caption)
    protect = get_setting("protect_content", "0") == "1"
    try:
        await context.bot.send_photo(
            q.message.chat_id, qr, caption=msg,
            parse_mode="HTML",
            reply_markup=kb,
            protect_content=False,
        )
    except TelegramError as e:
        logger.warning(f"HTML photo caption failed: {e}")
        await send_photo_msg(context.bot, q.message.chat_id, qr, msg, reply_markup=kb, force_protect=False)


async def verify_start(q, context, pid):
    await answer(q)
    plan = fetch_plan(pid)
    if plan is None:
        await reply_q(q, "❌ Plan not found")
        return
    context.user_data["verify_plan_id"] = pid
    context.user_data["verify_state"] = "awaiting_screenshot"
    try:
        await q.message.delete()
    except TelegramError:
        pass
    kb = InlineKeyboardMarkup([[btn("🔙 Back to Plans", "menu:home", style="primary")]])
    await send_msg(
        context.bot, q.message.chat_id,
        "📸 Step 1/2: Send Payment Screenshot\n\nUpload the screenshot of your UPI payment.",
        reply_markup=kb,
    )


async def verify_handle_message(update, context):
    state = context.user_data.get("verify_state")
    if not state:
        return False
    msg = update.message
    if state == "awaiting_screenshot":
        if not msg.photo:
            await reply_msg(update, "❌ Please send a photo")
            return True
        context.user_data["verify_screenshot"] = msg.photo[-1].file_id
        context.user_data["verify_state"] = "awaiting_utr"
        await reply_msg(update, "🧾 Step 2/2: Send UTR / Transaction ID\n\nEnter the UTR or Transaction ID from your payment app.")
        return True
    if state == "awaiting_utr":
        utr = (msg.text or "").strip()
        if len(utr) < 4:
            await reply_msg(update, "❌ Invalid UTR. Try again")
            return True
        pid = context.user_data.get("verify_plan_id")
        screenshot = context.user_data.get("verify_screenshot")
        plan = fetch_plan(pid) if pid else None
        if plan is None:
            for k in ("verify_state", "verify_plan_id", "verify_screenshot"):
                context.user_data.pop(k, None)
            await reply_msg(update, "❌ Plan no longer exists")
            return True
        con = db()
        try:
            existing = con.execute(
                "SELECT id FROM payments WHERE user_id=? AND plan_id=? AND status='pending'",
                (msg.from_user.id, pid),
            ).fetchone()
        finally:
            con.close()
        if existing:
            await reply_msg(update, f"⏳ Already pending #{existing['id']}")
            for k in ("verify_state", "verify_plan_id", "verify_screenshot"):
                context.user_data.pop(k, None)
            return True
        con = db()
        try:
            con.execute(
                "INSERT INTO payments(user_id, username, plan_id, plan_name, amount, validity, screenshot_file_id, utr, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    msg.from_user.id, msg.from_user.username or "", pid,
                    plan["name"], float(plan["price"]), int(plan["validity_days"]),
                    screenshot, utr, now_iso(),
                ),
            )
            payment_id = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        finally:
            con.close()
        for k in ("verify_state", "verify_plan_id", "verify_screenshot"):
            context.user_data.pop(k, None)
        caption = (
            "💳 NEW PAYMENT REQUEST\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 Plan: {esc(plan['name'])}\n"
            f"👤 User: {esc(safe_name(msg.from_user))}\n"
            f"🆔 ID: <code>{msg.from_user.id}</code>\n"
            f"💰 Amount: ₹{float(plan['price']):g}\n"
            f"🧾 UTR: <code>{esc(utr)}</code>\n"
            f"🧾 Payment ID: #{payment_id}"
        )
        kb = InlineKeyboardMarkup([
            [btn("✅ Approve", f"admin:pay:approve:{payment_id}", style="success"),
             btn("❌ Reject", f"admin:pay:reject:{payment_id}", style="danger")],
        ])
        for admin_id in current_admin_ids():
            try:
                await send_photo_msg(context.bot, admin_id, screenshot, caption, reply_markup=kb)
            except TelegramError as e:
                logger.warning(f"admin notify {admin_id} failed: {e}")
        await reply_msg(update, "✅ Payment submitted!\n\n⏳ Waiting for admin approval...")
        return True
    return False


async def start(update, context):
    if not update.message:
        return
    user = update.effective_user
    if user is None:
        return
    con = db()
    try:
        existing = con.execute("SELECT id FROM users WHERE id=?", (user.id,)).fetchone()
    finally:
        con.close()
    register_user(user)
    if existing is None:
        asyncio.create_task(_notify_admin_new_user(context.bot, user))
    if is_user_blocked(user.id):
        await reply_msg(update, "⛔ You are blocked.")
        return
    await send_start_screen(context.bot, update.effective_chat.id)


async def admin_cmd(update, context):
    if not update.message:
        return
    if not admin_only(update):
        await reply_msg(update, "⛔ Admin only")
        return
    await send_msg(
        context.bot, update.effective_chat.id,
        "👑 Admin Panel\nChoose an option below:",
        reply_markup=admin_main_keyboard(),
    )


async def done_cmd(update, context):
    if not update.message:
        return
    state = context.user_data.get("_manual_state")
    if state == "welcome_videos":
        tmp = context.user_data.get("wv_tmp") or []
        set_setting("welcome_videos", json.dumps(tmp))
        context.user_data.pop("_manual_state", None)
        context.user_data.pop("wv_tmp", None)
        await reply_msg(update, f"✅ Saved {len(tmp)} welcome videos")
        return
    if state == "demo_add":
        count = context.user_data.get("demo_count") or 0
        context.user_data.pop("_manual_state", None)
        context.user_data.pop("demo_count", None)
        await reply_msg(update, f"✅ Added {count} demo media")
        return
    await reply_msg(update, "ℹ️ Nothing to finish")


async def admin_dashboard(q, context):
    await answer(q)
    con = db()
    try:
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        today = datetime.now(timezone.utc).date().isoformat()
        new_today = con.execute("SELECT COUNT(*) c FROM users WHERE registered_at LIKE ?", (today + "%",)).fetchone()["c"]
        now = now_iso()
        premium = con.execute("SELECT COUNT(*) c FROM users WHERE premium_until IS NOT NULL AND premium_until > ?", (now,)).fetchone()["c"]
        pending = con.execute("SELECT COUNT(*) c FROM payments WHERE status='pending'").fetchone()["c"]
        approved = con.execute("SELECT COUNT(*) c FROM payments WHERE status='approved'").fetchone()["c"]
        rejected = con.execute("SELECT COUNT(*) c FROM payments WHERE status='rejected'").fetchone()["c"]
        revenue = con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved'").fetchone()["s"]
        today_rev = con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND approved_at LIKE ?", (today + "%",)).fetchone()["s"]
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        week_rev = con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND approved_at >= ?", (week_ago + "T00:00:00",)).fetchone()["s"]
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        month_rev = con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND approved_at >= ?", (month_ago + "T00:00:00",)).fetchone()["s"]
        best = con.execute("SELECT plan_name, COUNT(*) c FROM payments WHERE status='approved' GROUP BY plan_name ORDER BY c DESC LIMIT 1").fetchone()
    finally:
        con.close()
    best_txt = f"{best['plan_name']} ({best['c']})" if best else "-"
    msg = (
        "📊 DASHBOARD\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {users}\n"
        f"🆓 Free: {users - premium}\n"
        f"👑 Premium: {premium}\n"
        f"📅 New Today: {new_today}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ Pending: {pending}\n"
        f"✅ Approved: {approved}\n"
        f"❌ Rejected: {rejected}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Revenue: ₹{float(revenue):.2f}\n"
        f"📅 Today: ₹{float(today_rev):.2f}\n"
        f"📈 Week: ₹{float(week_rev):.2f}\n"
        f"📊 Month: ₹{float(month_rev):.2f}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 Best Plan: {best_txt}"
    )
    kb = InlineKeyboardMarkup([
        [btn("🔄 Refresh", "admin:dashboard", style="primary")],
        [btn("🔙 Back", "admin:menu", style="primary")],
    ])
    await edit_msg(q, msg, reply_markup=kb)


async def admin_plans_view(q, context):
    await answer(q)
    plans = fetch_all_plans()
    rows = []
    if not plans:
        rows.append([btn("➕ Add Plan", "admin:plans:add", style="success")])
    for p in plans:
        link_ok = "🔗" if p["premium_link"] else "⚠️"
        price = float(p["price"])
        rows.append([
            btn(f"{link_ok} {p['name']} • ₹{price:g}", f"admin:plan:view:{p['id']}", style="primary"),
            btn("✏️", f"admin:plan:edit:{p['id']}", style="success"),
            btn("🗑️", f"admin:plan:delete:{p['id']}", style="danger"),
        ])
    rows.append([btn("🔙 Back", "admin:plans", style="primary")])
    await edit_msg(q, "📋 All Plans", reply_markup=InlineKeyboardMarkup(rows))


async def admin_plan_detail(q, context, pid):
    await answer(q)
    p = fetch_plan(pid)
    if p is None:
        await answer(q, "Not found", True)
        return
    link = p["premium_link"] or "-"
    validity = "Lifetime" if int(p["validity_days"]) >= LIFETIME_DAYS else f"{p['validity_days']} days"
    msg = (
        f"💎 {esc(p['name'])}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: ₹{float(p['price']):g}\n"
        f"⏳ Validity: {validity}\n"
        f"🔗 Link: <code>{esc(link)}</code>\n"
        f"📌 Enabled: {'✅' if p['enabled'] else '❌'}"
    )
    kb = InlineKeyboardMarkup([
        [btn("✏️ Edit", f"admin:plan:edit:{pid}", style="success")],
        [btn("🗑️ Delete", f"admin:plan:delete:{pid}", style="danger")],
        [btn("🔙 Back", "admin:plans:view", style="primary")],
    ])
    await edit_msg(q, msg, reply_markup=kb)


async def admin_payments(q, context):
    await answer(q)
    con = db()
    try:
        rows = con.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 30").fetchall()
    finally:
        con.close()
    lines = ["💳 Recent Payments", "━━━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        uname = ("@" + r["username"]) if r["username"] else f"id{r['user_id']}"
        lines.append(f"🧾 #{r['id']} | {esc(uname)} | {esc(r['plan_name'])} | ₹{float(r['amount'] or 0):g} | {r['status'].upper()}")
    if not rows:
        lines.append("No payments yet")
    kb = InlineKeyboardMarkup([[btn("🔙 Back", "admin:menu", style="primary")]])
    await edit_msg(q, "\n".join(lines), reply_markup=kb)


async def admin_pay_approve(q, context, pid):
    await answer(q)
    payment = fetch_payment(pid)
    if payment is None:
        await answer(q, "Payment not found", True)
        return
    if payment["status"] != "pending":
        await answer(q, f"Already {payment['status']}", True)
        return
    uid = payment["user_id"]
    validity = int(payment["validity"] or 30)
    con = db()
    try:
        r = con.execute("SELECT premium_until FROM users WHERE id=?", (uid,)).fetchone()
        base = datetime.now(timezone.utc)
        if r and r["premium_until"]:
            try:
                pu = datetime.fromisoformat(r["premium_until"])
                if pu > base:
                    base = pu
            except Exception:
                pass
        new_until = (base + timedelta(days=validity)).isoformat()
        con.execute("UPDATE users SET premium_until=? WHERE id=?", (new_until, uid))
        con.execute("UPDATE payments SET status='approved', approved_at=? WHERE id=?", (now_iso(), pid))
    finally:
        con.close()
    plan = fetch_plan(payment["plan_id"])
    link = ""
    if plan and plan["premium_link"]:
        link = plan["premium_link"]
    if not link:
        link = get_setting("premium_channel", "") or ""
    if link.startswith("@"):
        link = f"https://t.me/{link.lstrip('@')}"
    caption = (
        "🎉 Purchase Successful!\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 Plan: {esc(payment['plan_name'])}\n"
        f"💰 Amount: ₹{float(payment['amount'] or 0):g}\n"
        f"🧾 Payment ID: #{pid}\n"
        f"🧾 UTR: {esc(payment['utr'])}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Payment verified\n"
        "🔐 Premium access activated!"
    )
    rows = []
    if link:
        rows.append([btn("🔓 Join Premium Now", url=link, style="success")])
    kb = InlineKeyboardMarkup(rows) if rows else None
    try:
        if payment["screenshot_file_id"]:
            await send_photo_msg(context.bot, uid, payment["screenshot_file_id"], caption, reply_markup=kb)
        else:
            await send_msg(context.bot, uid, caption, reply_markup=kb)
    except TelegramError as e:
        logger.warning(f"user notify failed: {e}")
    try:
        await _broadcast_new_purchase(
            context.bot,
            payment["plan_name"] or "",
            float(payment["amount"] or 0),
            pid,
            payment["screenshot_file_id"],
        )
    except Exception as e:
        logger.warning(f"broadcast failed: {e}")
    await answer(q, "Approved ✅", True)
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[btn("✅ APPROVED", "noop", style="success")]]))
    except TelegramError:
        pass


async def _run_broadcast(bot, content, target, admin_chat):
    con = db()
    try:
        if target == "premium":
            now = now_iso()
            rows = con.execute("SELECT id FROM users WHERE status='active' AND premium_until IS NOT NULL AND premium_until > ?", (now,)).fetchall()
        elif target == "free":
            now = now_iso()
            rows = con.execute("SELECT id FROM users WHERE status='active' AND (premium_until IS NULL OR premium_until <= ?)", (now,)).fetchall()
        else:
            rows = con.execute("SELECT id FROM users WHERE status='active'").fetchall()
    finally:
        con.close()
    ids = [r["id"] for r in rows]
    total = len(ids)
    success = 0
    failed = 0
    sem = asyncio.Semaphore(20)
    async def send_one(uid):
        nonlocal success, failed
        async with sem:
            for _ in range(3):
                try:
                    await _send_content(bot, uid, content)
                    success += 1
                    return
                except RetryAfter as e:
                    await asyncio.sleep(float(e.retry_after) + 0.5)
                except TelegramError:
                    failed += 1
                    return
            failed += 1
    await asyncio.gather(*(send_one(uid) for uid in ids))
    try:
        await send_msg(bot, admin_chat, f"📢 Broadcast Complete\n\n👥 Total: {total}\n✅ Success: {success}\n❌ Failed: {failed}")
    except TelegramError:
        pass


async def _send_content(bot, chat_id, content):
    ctype = content.get("type")
    file_id = content.get("file_id")
    text = content.get("text") or ""
    if ctype == "text":
        clean, ents = build_entities(text)
        await bot.send_message(chat_id, clean, entities=ents)
    elif ctype == "photo":
        clean, ents = build_entities(text)
        await bot.send_photo(chat_id, file_id, caption=clean, caption_entities=ents)
    elif ctype == "video":
        clean, ents = build_entities(text)
        await bot.send_video(chat_id, file_id, caption=clean, caption_entities=ents)
    elif ctype == "document":
        clean, ents = build_entities(text)
        await bot.send_document(chat_id, file_id, caption=clean, caption_entities=ents)
    elif ctype == "audio":
        clean, ents = build_entities(text)
        await bot.send_audio(chat_id, file_id, caption=clean, caption_entities=ents)
    elif ctype == "voice":
        await bot.send_voice(chat_id, file_id)
    elif ctype == "animation":
        clean, ents = build_entities(text)
        await bot.send_animation(chat_id, file_id, caption=clean, caption_entities=ents)
    elif ctype == "sticker":
        await bot.send_sticker(chat_id, file_id)
    else:
        await bot.send_message(chat_id, text or "")


async def backup_sqlite(q, context):
    await answer(q, "Backing up...")
    tmpdir = tempfile.mkdtemp(prefix="bbackup_")
    dest = os.path.join(tmpdir, "next.sqlite")
    src = db()
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    try:
        with open(dest, "rb") as f:
            await context.bot.send_document(q.message.chat_id, f, filename="next.sqlite", caption="💾 SQLite Backup")
    except TelegramError as e:
        logger.exception(f"backup send failed: {e}")
        await answer(q, "❌ Backup failed", True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def export_database(q, context):
    await answer(q, "Preparing...")
    tmp = tempfile.mkdtemp(prefix="bexport_")
    try:
        db_dest = os.path.join(tmp, "next.sqlite")
        src = db()
        out = sqlite3.connect(db_dest)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()

        media_dir = os.path.join(tmp, "media")
        os.makedirs(media_dir, exist_ok=True)
        refs = []
        con = db()
        try:
            for r in con.execute("SELECT id, media_type, file_id FROM demo_media"):
                if r["file_id"]:
                    refs.append(("demo", r["id"], r["file_id"], r["media_type"] or "doc"))
            for r in con.execute("SELECT id, screenshot_file_id FROM payments WHERE screenshot_file_id IS NOT NULL"):
                refs.append(("payment", r["id"], r["screenshot_file_id"], "photo"))
            for r in con.execute("SELECT id, image_file_id FROM plans WHERE image_file_id IS NOT NULL"):
                refs.append(("plan", r["id"], r["image_file_id"], "photo"))
            wi = con.execute("SELECT value FROM settings WHERE key='welcome_image'").fetchone()
            if wi and wi["value"]:
                refs.append(("setting", "welcome_image", wi["value"], "photo"))
            wv = con.execute("SELECT value FROM settings WHERE key='welcome_videos'").fetchone()
            if wv and wv["value"]:
                try:
                    arr = json.loads(wv["value"])
                except Exception:
                    arr = []
                for idx, fid in enumerate(arr):
                    if fid:
                        refs.append(("setting", f"welcome_videos:{idx}", fid, "video"))
        finally:
            con.close()

        manifest = []
        ok = 0
        fail = 0
        seen = set()
        for kind, ref, fid, mtype in refs:
            if not fid or fid in seen:
                continue
            seen.add(fid)
            ext = {"photo": ".jpg", "video": ".mp4", "animation": ".mp4", "audio": ".mp3", "voice": ".ogg", "doc": ".bin"}.get(mtype, ".bin")
            safe_ref = str(ref).replace(":", "_").replace("/", "_")
            fname = f"{kind}_{safe_ref}{ext}"
            path = os.path.join(media_dir, fname)
            try:
                tgfile = await context.bot.get_file(fid)
                await tgfile.download_to_drive(path)
                manifest.append({"file_id": fid, "kind": kind, "ref": ref, "path": os.path.join("media", fname), "type": mtype})
                ok += 1
            except Exception as e:
                logger.warning(f"export media failed {fid}: {e}")
                fail += 1
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(tempfile.gettempdir(), f"export_{ts}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(tmp):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, tmp)
                    zf.write(full, arc)

        total_size = os.path.getsize(zip_path)
        size_kb = total_size // 1024
        LIMIT = 49 * 1024 * 1024

        if total_size <= LIMIT:
            with open(zip_path, "rb") as f:
                await context.bot.send_document(
                    q.message.chat_id, f, filename=f"export_{ts}.zip",
                    caption=f"📤 Export Complete\n\n✅ Media: {ok}\n❌ Failed: {fail}\n📦 Size: {size_kb} KB",
                )
        else:
            await send_msg(
                context.bot, q.message.chat_id,
                f"📦 ZIP is {size_kb} KB (>49 MB). Splitting into parts...\n✅ Media: {ok} | ❌ Failed: {fail}"
            )
            parts = []
            part_num = 1
            with open(zip_path, "rb") as f:
                while True:
                    chunk = f.read(LIMIT)
                    if not chunk:
                        break
                    part_path = os.path.join(tempfile.gettempdir(), f"export_{ts}_part{part_num}.zip")
                    with open(part_path, "wb") as pf:
                        pf.write(chunk)
                    parts.append(part_path)
                    part_num += 1
            for idx, p in enumerate(parts, 1):
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_document(
                            q.message.chat_id, f,
                            filename=f"export_{ts}_part{idx}of{len(parts)}.zip",
                            caption=f"📦 Part {idx}/{len(parts)} — {os.path.getsize(p)//1024} KB",
                        )
                except TelegramError as e:
                    logger.warning(f"part {idx} send failed: {e}")
                finally:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            await send_msg(
                context.bot, q.message.chat_id,
                f"✅ Sent {len(parts)} parts.\n💡 Import: saare parts ek folder me rakhke `cat part*.zip > full.zip` chalao, phir import karo."
            )

        try:
            os.remove(zip_path)
        except OSError:
            pass
    except Exception as e:
        logger.exception(f"export failed: {e}")
        try:
            await answer(q, "❌ Export failed", True)
        except TelegramError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



async def _reupload_media(bot, kind, local_path):
    mtype = "photo"
    if kind in ("demo",):
        if local_path.lower().endswith((".mp4", ".mov")):
            mtype = "video"
    with open(local_path, "rb") as f:
        if mtype == "video":
            m = await bot.send_video(ADMIN_ID, f)
            fid = m.video.file_id
            try:
                await m.delete()
            except TelegramError:
                pass
            return fid
        else:
            m = await bot.send_photo(ADMIN_ID, f)
            fid = m.photo[-1].file_id
            try:
                await m.delete()
            except TelegramError:
                pass
            return fid


async def import_from_zip(update, context, document):
    msg = update.message
    tmp = tempfile.mkdtemp(prefix="bimport_")
    backup = DB_FILE + ".before_import"
    try:
        tgfile = await context.bot.get_file(document.file_id)
        local = os.path.join(tmp, document.file_name or "import.bin")
        await tgfile.download_to_drive(local)
        if zipfile.is_zipfile(local):
            with zipfile.ZipFile(local, "r") as zf:
                zf.extractall(tmp)
            candidates = []
            for root, _dirs, files in os.walk(tmp):
                for fn in files:
                    if fn.endswith(".sqlite"):
                        candidates.append(os.path.join(root, fn))
            if not candidates:
                await reply_msg(update, "❌ No SQLite file in ZIP")
                return
            new_db = candidates[0]
        else:
            new_db = local
        con = sqlite3.connect(new_db)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                await reply_msg(update, "❌ DB integrity check failed")
                return
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            required = {"users", "plans", "payments", "settings"}
            if not required.issubset(tables):
                await reply_msg(update, f"❌ Missing tables: {required - tables}")
                return
        finally:
            con.close()
        if os.path.exists(DB_FILE):
            shutil.copy2(DB_FILE, backup)
        shutil.copy2(new_db, DB_FILE)
        init_db()
        manifest_path = os.path.join(tmp, "manifest.json")
        reup = 0
        failed = 0
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            for item in manifest:
                old_fid = item.get("file_id")
                rel = item.get("path")
                kind = item.get("kind")
                if not old_fid or not rel:
                    continue
                local_media = os.path.join(tmp, rel)
                if not os.path.exists(local_media):
                    failed += 1
                    continue
                try:
                    new_fid = await _reupload_media(context.bot, kind, local_media)
                    con = db()
                    try:
                        con.execute("UPDATE demo_media SET file_id=? WHERE file_id=?", (new_fid, old_fid))
                        con.execute("UPDATE payments SET screenshot_file_id=? WHERE screenshot_file_id=?", (new_fid, old_fid))
                        con.execute("UPDATE plans SET image_file_id=? WHERE image_file_id=?", (new_fid, old_fid))
                        con.execute("UPDATE settings SET value=? WHERE value=?", (new_fid, old_fid))
                        wv = con.execute("SELECT value FROM settings WHERE key='welcome_videos'").fetchone()
                        if wv and wv["value"]:
                            try:
                                arr = json.loads(wv["value"])
                                changed = False
                                for i, v in enumerate(arr):
                                    if v == old_fid:
                                        arr[i] = new_fid
                                        changed = True
                                if changed:
                                    con.execute("UPDATE settings SET value=? WHERE key='welcome_videos'", (json.dumps(arr),))
                            except Exception:
                                pass
                    finally:
                        con.close()
                    reup += 1
                except Exception as e:
                    logger.warning(f"reupload failed {old_fid}: {e}")
                    failed += 1
        try:
            await msg.delete()
        except TelegramError:
            pass
        await reply_msg(update, f"✅ Import complete\n📤 Re-uploaded: {reup}\n❌ Failed: {failed}")
        try:
            if os.path.exists(backup):
                os.remove(backup)
        except OSError:
            pass
    except Exception as e:
        logger.exception(f"import failed: {e}")
        if os.path.exists(backup):
            try:
                shutil.copy2(backup, DB_FILE)
                init_db()
            except Exception as e2:
                logger.error(f"restore failed: {e2}")
        await reply_msg(update, f"❌ Import failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def handle_admin_callback(q, context, data):
    if data == "admin:menu":
        await answer(q)
        try:
            await edit_msg(q, "👑 Admin Panel\nChoose an option below:", reply_markup=admin_main_keyboard())
        except TelegramError:
            await send_msg(context.bot, q.message.chat_id, "👑 Admin Panel\nChoose an option below:", reply_markup=admin_main_keyboard())
        return
    if data == "admin:dashboard":
        await admin_dashboard(q, context)
        return
    if data == "admin:plans":
        await answer(q)
        kb = InlineKeyboardMarkup([
            [btn("➕ Add New Plan", "admin:plans:add", style="success")],
            [btn("📋 View / Edit Plans", "admin:plans:view", style="primary")],
            [btn("🖼️ Set Plan Photo", "admin:plans:photo", style="primary")],
            [btn("🔙 Back", "admin:menu", style="primary")],
        ])
        await edit_msg(q, "💎 Plans Management", reply_markup=kb)
        return
    if data == "admin:plans:view":
        await admin_plans_view(q, context)
        return
    if data.startswith("admin:plan:view:"):
        await admin_plan_detail(q, context, int(data.rsplit(":", 1)[1]))
        return
    if data == "admin:plans:add":
        await answer(q)
        context.user_data["_manual_state"] = "plan_name"
        await edit_msg(q, "📝 Step 1/5 — Send Plan Name:", reply_markup=back_kb("admin:plans"))
        return
    if data.startswith("admin:plan:edit:"):
        await answer(q)
        pid = int(data.rsplit(":", 1)[1])
        context.user_data["edit_plan_id"] = pid
        context.user_data["_manual_state"] = "edit_plan_name"
        await edit_msg(q, "📝 Send new Plan Name (or 'skip'):", reply_markup=back_kb("admin:plans:view"))
        return
    if data.startswith("admin:plan:delete:"):
        await answer(q)
        pid = int(data.rsplit(":", 1)[1])
        con = db()
        try:
            con.execute("DELETE FROM plans WHERE id=?", (pid,))
        finally:
            con.close()
        await answer(q, "Deleted", True)
        await admin_plans_view(q, context)
        return
    if data == "admin:plans:photo":
        await answer(q)
        context.user_data["_manual_state"] = "plan_photo"
        await edit_msg(q, "🖼️ Send plan photo:", reply_markup=back_kb("admin:plans"))
        return
    if data == "admin:users":
        await answer(q)
        context.user_data["_manual_state"] = "user_search"
        await edit_msg(q, "👥 Send User ID or @username to search:", reply_markup=back_kb("admin:menu"))
        return
    if data.startswith("admin:user:add30:"):
        await answer(q)
        uid = int(data.rsplit(":", 1)[1])
        con = db()
        try:
            r = con.execute("SELECT premium_until FROM users WHERE id=?", (uid,)).fetchone()
            if r is None:
                await answer(q, "User not found", True)
                return
            base = datetime.now(timezone.utc)
            if r["premium_until"]:
                try:
                    pu = datetime.fromisoformat(r["premium_until"])
                    if pu > base:
                        base = pu
                except Exception:
                    pass
            new_until = (base + timedelta(days=30)).isoformat()
            con.execute("UPDATE users SET premium_until=? WHERE id=?", (new_until, uid))
        finally:
            con.close()
        await answer(q, "✅ +30 days added", True)
        return
    if data.startswith("admin:user:remove:"):
        await answer(q)
        uid = int(data.rsplit(":", 1)[1])
        con = db()
        try:
            con.execute("UPDATE users SET premium_until=NULL WHERE id=?", (uid,))
        finally:
            con.close()
        await answer(q, "✅ Premium removed", True)
        return
    if data.startswith("admin:user:block:"):
        await answer(q)
        uid = int(data.rsplit(":", 1)[1])
        con = db()
        try:
            r = con.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
            cur = (r["status"] if r else "active") or "active"
            new_status = "blocked" if cur == "active" else "active"
            con.execute("UPDATE users SET status=? WHERE id=?", (new_status, uid))
        finally:
            con.close()
        await answer(q, f"✅ {new_status}", True)
        return
    if data == "admin:payments":
        await admin_payments(q, context)
        return
    if data.startswith("admin:pay:approve:"):
        await admin_pay_approve(q, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("admin:pay:reject:"):
        await answer(q)
        pid = int(data.rsplit(":", 1)[1])
        context.user_data["reject_id"] = pid
        context.user_data["_manual_state"] = "reject_reason"
        await send_msg(context.bot, q.message.chat_id, "📝 Send rejection reason:", reply_markup=back_kb("admin:payments"))
        return
    if data == "admin:settings":
        await answer(q)
        rows = [
            [btn("🏦 UPI ID", "admin:set:upi_id", style="primary")],
            [btn("🔓 Premium Fallback", "admin:set:premium_channel", style="primary")],
            [btn("🆘 Support Contact", "admin:set:support_id", style="primary")],
            [btn("🏪 Proof / Delivery Channel", "admin:set:proof_channel", style="primary")],
            [btn("📖 How to Buy Message", "admin:set:howtobuy_msg", style="primary")],
            [btn("🔙 Back", "admin:menu", style="primary")],
        ]
        await edit_msg(q, "⚙️ Bot Settings", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith("admin:set:"):
        await answer(q)
        key = data.split(":", 2)[2]
        context.user_data["_manual_state"] = f"set:{key}"
        await edit_msg(q, f"📝 Send value for {key}:", reply_markup=back_kb("admin:settings"))
        return
    if data == "admin:welcome":
        await answer(q)
        rows = [
            [btn("✏️ Welcome Text", "admin:welcome:text", style="primary")],
            [btn("🖼️ Welcome Image", "admin:welcome:image", style="primary")],
            [btn("🎥 5 Welcome Videos", "admin:welcome:videos", style="primary")],
            [btn("👁️ Preview", "admin:welcome:preview", style="success")],
            [btn("🗑️ Remove Image", "admin:welcome:rm_image", style="danger"), btn("🗑️ Remove Videos", "admin:welcome:rm_videos", style="danger")],
            [btn("🔙 Back", "admin:menu", style="primary")],
        ]
        await edit_msg(q, "🖼️ Welcome Settings", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data == "admin:welcome:text":
        await answer(q)
        context.user_data["_manual_state"] = "set:welcome_text"
        await edit_msg(q, "📝 Send welcome text:", reply_markup=back_kb("admin:welcome"))
        return
    if data == "admin:welcome:image":
        await answer(q)
        context.user_data["_manual_state"] = "welcome_image"
        await edit_msg(q, "🖼️ Send welcome image:", reply_markup=back_kb("admin:welcome"))
        return
    if data == "admin:welcome:videos":
        await answer(q)
        context.user_data["_manual_state"] = "welcome_videos"
        context.user_data["wv_tmp"] = []
        await edit_msg(q, "🎥 Send up to 5 videos one by one. Send /done to finish.", reply_markup=back_kb("admin:welcome"))
        return
    if data == "admin:welcome:preview":
        await answer(q, "Sending preview...")
        await send_start_screen(context.bot, q.message.chat_id)
        return
    if data == "admin:welcome:rm_image":
        await answer(q)
        set_setting("welcome_image", "")
        await answer(q, "✅ Image removed", True)
        return
    if data == "admin:welcome:rm_videos":
        await answer(q)
        set_setting("welcome_videos", "")
        await answer(q, "✅ Videos removed", True)
        return
    if data == "admin:demo":
        await answer(q)
        plans = fetch_all_plans()
        rows = [[btn("🌐 Global Demo (All Plans)", "admin:demo:plan:0", style="primary")]]
        for p in plans:
            rows.append([btn(f"💎 {p['name']}", f"admin:demo:plan:{p['id']}", style="success")])
        rows.append([btn("🗑️ Clear ALL Demos", "admin:demo:clear", style="danger")])
        rows.append([btn("🔙 Back", "admin:menu", style="primary")])
        await edit_msg(q, "🥵 Demo Videos\n\nPehle plan select karo:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("admin:demo:plan:"):
        await answer(q)
        pid = int(data.rsplit(":", 1)[1])
        context.user_data["demo_plan_id"] = pid
        title = "🌐 Global Demo" if pid == 0 else f"💎 Plan #{pid} Demo"
        rows = [
            [btn("➕ Add Demo", "admin:demo:add", style="success")],
            [btn("📋 View This Plan's Demos", "admin:demo:view", style="primary")],
            [btn("🗑️ Clear This Plan's Demos", "admin:demo:clear_plan", style="danger")],
            [btn("🔙 Back", "admin:demo", style="primary")],
        ]
        await edit_msg(q, f"🥵 {title}\n\nAdd/View/Clear:", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data == "admin:demo:add":
        await answer(q)
        context.user_data["_manual_state"] = "demo_add"
        context.user_data["demo_count"] = 0
        await edit_msg(
            q,
            "📤 Send demo media — video, photo, GIF, or document.\n"
            "✅ Mix allowed — send any type.\n\n"
            "Send /done to finish.",
            reply_markup=back_kb("admin:demo"),
        )
        return
    if data == "admin:demo:view":
        await answer(q)
        pid = context.user_data.get("demo_plan_id", 0)
        con = db()
        try:
            if pid:
                rows = con.execute("SELECT * FROM demo_media WHERE plan_id=? ORDER BY sort_order ASC, id ASC", (pid,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM demo_media WHERE plan_id IS NULL ORDER BY sort_order ASC, id ASC").fetchall()
        finally:
            con.close()
        if not rows:
            await answer(q, "No demos", True)
            return
        for r in rows:
            try:
                if r["media_type"] == "video":
                    await context.bot.send_video(q.message.chat_id, r["file_id"], has_spoiler=True)
                elif r["media_type"] == "photo":
                    await context.bot.send_photo(q.message.chat_id, r["file_id"], has_spoiler=True)
                elif r["media_type"] == "animation":
                    await context.bot.send_animation(q.message.chat_id, r["file_id"], has_spoiler=True)
                else:
                    await context.bot.send_document(q.message.chat_id, r["file_id"])
            except TelegramError as e:
                logger.warning(f"demo send failed: {e}")
        return
    if data == "admin:demo:clear_plan":
        await answer(q)
        pid = context.user_data.get("demo_plan_id", 0)
        con = db()
        try:
            if pid:
                con.execute("DELETE FROM demo_media WHERE plan_id=?", (pid,))
            else:
                con.execute("DELETE FROM demo_media WHERE plan_id IS NULL")
        finally:
            con.close()
        await answer(q, "✅ Cleared", True)
        return
    if data == "admin:demo:clear":
        await answer(q)
        con = db()
        try:
            con.execute("DELETE FROM demo_media")
        finally:
            con.close()
        await answer(q, "✅ Cleared", True)
        return
    if data == "admin:broadcast":
        await answer(q)
        rows = [
            [btn("👥 All Users", "admin:broadcast:target:all", style="primary")],
            [btn("💎 Premium Only", "admin:broadcast:target:premium", style="success")],
            [btn("🆓 Free Only", "admin:broadcast:target:free", style="primary")],
            [btn("🔙 Back", "admin:menu", style="primary")],
        ]
        await edit_msg(q, "📢 Broadcast\nChoose target audience:", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith("admin:broadcast:target:"):
        await answer(q)
        target = data.rsplit(":", 1)[1]
        context.user_data["bc_target"] = target
        context.user_data["_manual_state"] = "broadcast_content"
        await edit_msg(q, f"📢 Send broadcast content (target: {target}):", reply_markup=back_kb("admin:broadcast"))
        return
    if data == "admin:broadcast:send":
        await answer(q)
        content = context.user_data.get("bc_content")
        target = context.user_data.get("bc_target", "all")
        if not content:
            await answer(q, "❌ No content", True)
            return
        await answer(q, "📢 Broadcast started in background", True)
        asyncio.create_task(_run_broadcast(context.bot, content, target, q.message.chat_id))
        context.user_data.pop("bc_content", None)
        context.user_data.pop("_manual_state", None)
        return
    if data == "admin:broadcast:cancel":
        await answer(q)
        context.user_data.pop("bc_content", None)
        context.user_data.pop("_manual_state", None)
        await edit_msg(q, "❌ Broadcast cancelled", reply_markup=back_kb("admin:broadcast"))
        return
    if data == "admin:protect":
        await answer(q)
        cur = get_setting("protect_content", "0")
        new_val = "0" if cur == "1" else "1"
        set_setting("protect_content", new_val)
        status = "🟢 ON" if new_val == "1" else "🔴 OFF"
        kb = InlineKeyboardMarkup([
            [btn(f"🛡 Content Protection: {status}", "admin:protect", style="primary")],
            [btn("🔙 Back", "admin:menu", style="primary")],
        ])
        info = (
            "🛡 CONTENT PROTECTION\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {status}\n"
            "\n"
            "When ON:\n"
            "❌ No forwarding\n"
            "❌ No copy text\n"
            "❌ No save media\n"
            "\n"
            "Tap button to toggle."
        )
        try:
            await edit_msg(q, info, reply_markup=kb)
        except TelegramError:
            await send_msg(context.bot, q.message.chat_id, info, reply_markup=kb)
        return
    if data == "admin:change_name":
        await answer(q)
        context.user_data["_manual_state"] = "bot_name"
        await edit_msg(q, "🏷 Send new Bot Name:", reply_markup=back_kb("admin:menu"))
        return
    if data == "admin:change_photo":
        await answer(q)
        context.user_data["_manual_state"] = "bot_photo"
        await edit_msg(q, "🖼 Send new Bot Profile Photo:", reply_markup=back_kb("admin:menu"))
        return
    if data == "admin:clone_bot":
        await answer(q)
        context.user_data["_manual_state"] = "clone_token"
        await edit_msg(q, "🧬 Send NEW Bot Token from BotFather:", reply_markup=back_kb("admin:menu"))
        return
    if data == "admin:security":
        await answer(q)
        ids = sorted(current_admin_ids())
        txt = "👑 Admin Management\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(f"🆔 <code>{i}</code>" for i in ids)
        rows = [
            [btn("➕ Add Admin", "admin:security:add", style="success")],
            [btn("➖ Remove Admin", "admin:security:remove", style="danger")],
            [btn("🔙 Back", "admin:menu", style="primary")],
        ]
        await edit_msg(q, txt, reply_markup=InlineKeyboardMarkup(rows))
        return
    if data == "admin:security:add":
        await answer(q)
        context.user_data["_manual_state"] = "admin_add"
        await edit_msg(q, "➕ Send user ID to add as admin:", reply_markup=back_kb("admin:security"))
        return
    if data == "admin:security:remove":
        await answer(q)
        context.user_data["_manual_state"] = "admin_remove"
        await edit_msg(q, "➖ Send user ID to remove from admins:", reply_markup=back_kb("admin:security"))
        return
    if data == "admin:db:export":
        await export_database(q, context)
        return
    if data == "admin:db:import":
        await answer(q)
        context.user_data["_manual_state"] = "db_import"
        await edit_msg(q, "📥 Send ZIP or SQLite file to import:", reply_markup=back_kb("admin:menu"))
        return
    if data == "admin:db:backup":
        await backup_sqlite(q, context)
        return
    await answer(q, "Unknown action", True)


async def callback(update, context):
    q = update.callback_query
    data = q.data or ""
    if data.startswith("noop"):
        await answer(q)
        return
    # Delete welcome media on any button click
    asyncio.create_task(_delete_welcome_media(context.bot, q.message.chat_id))
    # Auto delete prev plan demo batch on next action
    if not data.startswith("admin:"):
        asyncio.create_task(delete_plan_demo_msgs(context.bot, q.message.chat_id, context))
    if not data.startswith("admin:"):
        if is_user_blocked(q.from_user.id):
            await answer(q, "⛔ Blocked", True)
            return
    try:
        user_actions = {
            "menu:home": menu_home, "menu:howtobuy": menu_howtobuy,
            "menu:support": menu_support, "menu:support:contact": menu_support_contact,
            "menu:proof": menu_proof,
        }
        if data in user_actions:
            await user_actions[data](q, context)
        elif data.startswith("plan:view:"):
            await plan_view(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("plan:pay:"):
            await plan_pay(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("plan:verify:"):
            await verify_start(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("admin:"):
            if not admin_only(update):
                await answer(q, "⛔ Admin only", True)
                return
            await handle_admin_callback(q, context, data)
        else:
            await answer(q, "Invalid", True)
    except Exception as e:
        logger.exception("callback error")
        log_event("callback error", str(e))
        try:
            await answer(q, "❌ Error", True)
        except TelegramError:
            pass


async def handle_admin_state(update, context, state):
    msg = update.message
    text = (msg.text or "").strip()
    if state == "plan_name":
        context.user_data["new_plan_name"] = text
        context.user_data["_manual_state"] = "plan_price"
        await reply_msg(update, "💰 Step 2/5 — Send Price (₹):")
        return True
    if state == "plan_price":
        try:
            price = float(text)
        except ValueError:
            await reply_msg(update, "❌ Send a number")
            return True
        context.user_data["new_plan_price"] = price
        context.user_data["_manual_state"] = "plan_link"
        await reply_msg(update, "🔗 Step 3/5 — Send Premium Link (or 'skip'):")
        return True
    if state == "plan_link":
        link = "" if text.lower() == "skip" else text
        context.user_data["new_plan_link"] = link
        context.user_data["_manual_state"] = "plan_msg"
        await reply_msg(update, "💎 Step 4/5 — Send Plan Detail message (or 'skip'):")
        return True
    if state == "plan_msg":
        pmsg = "" if text.lower() == "skip" else text
        context.user_data["new_plan_msg"] = pmsg
        context.user_data["_manual_state"] = "plan_paymsg"
        await reply_msg(update, "💳 Step 5/5 — Send Pay Now message (or 'skip'):")
        return True
    if state == "plan_paymsg":
        paymsg = "" if text.lower() == "skip" else text
        name = context.user_data.pop("new_plan_name", "Plan")
        price = context.user_data.pop("new_plan_price", 0)
        link = context.user_data.pop("new_plan_link", "")
        pmsg = context.user_data.pop("new_plan_msg", "")
        con = db()
        try:
            con.execute(
                "INSERT INTO plans(name, price, validity_days, premium_link, plan_message, pay_message, enabled) VALUES(?, ?, ?, ?, ?, ?, 1)",
                (name, price, LIFETIME_DAYS, link, pmsg, paymsg),
            )
        finally:
            con.close()
        context.user_data.pop("_manual_state", None)
        await reply_msg(update, f"✅ Plan created: {name}")
        return True
    if state == "edit_plan_name":
        pid = context.user_data.get("edit_plan_id")
        if text.lower() != "skip":
            con = db()
            try:
                con.execute("UPDATE plans SET name=? WHERE id=?", (text, pid))
            finally:
                con.close()
        context.user_data["_manual_state"] = "edit_plan_price"
        await reply_msg(update, "💰 Send new Price (or 'skip'):")
        return True
    if state == "edit_plan_price":
        pid = context.user_data.get("edit_plan_id")
        if text.lower() != "skip":
            try:
                price = float(text)
                con = db()
                try:
                    con.execute("UPDATE plans SET price=? WHERE id=?", (price, pid))
                finally:
                    con.close()
            except ValueError:
                await reply_msg(update, "❌ Invalid number")
                return True
        context.user_data["_manual_state"] = "edit_plan_link"
        await reply_msg(update, "🔗 Send new Premium Link (or 'skip'):")
        return True
    if state == "edit_plan_link":
        pid = context.user_data.get("edit_plan_id")
        if text.lower() != "skip":
            con = db()
            try:
                con.execute("UPDATE plans SET premium_link=? WHERE id=?", (text, pid))
            finally:
                con.close()
        context.user_data["_manual_state"] = "edit_plan_msg"
        await reply_msg(update, "💎 Send new Plan Detail (or 'skip'):")
        return True
    if state == "edit_plan_msg":
        pid = context.user_data.get("edit_plan_id")
        if text.lower() != "skip":
            con = db()
            try:
                con.execute("UPDATE plans SET plan_message=? WHERE id=?", (text, pid))
            finally:
                con.close()
        context.user_data["_manual_state"] = "edit_plan_paymsg"
        await reply_msg(update, "💳 Send new Pay message (or 'skip'):")
        return True
    if state == "edit_plan_paymsg":
        pid = context.user_data.get("edit_plan_id")
        if text.lower() != "skip":
            con = db()
            try:
                con.execute("UPDATE plans SET pay_message=? WHERE id=?", (text, pid))
            finally:
                con.close()
        context.user_data.pop("_manual_state", None)
        context.user_data.pop("edit_plan_id", None)
        await reply_msg(update, "✅ Plan updated")
        return True
    if state.startswith("set:"):
        key = state.split(":", 1)[1]
        set_setting(key, text)
        context.user_data.pop("_manual_state", None)
        await reply_msg(update, f"✅ Saved {key}")
        return True
    if state == "clone_token":
        token = text.strip()
        if ":" not in token:
            await reply_msg(update, "❌ Invalid Token format.")
            return True
        context.user_data["clone_token"] = token
        context.user_data["_manual_state"] = "clone_admin"
        await reply_msg(update, "🧬 Send NEW Admin ID (User ID):")
        return True
    if state == "clone_admin":
        admin_id = text.strip()
        if not admin_id.isdigit():
            await reply_msg(update, "❌ Invalid User ID.")
            return True
        token = context.user_data.get("clone_token")
        await reply_msg(update, "⏳ Cloning process started...")
        import subprocess
        try:
            res = subprocess.run(["python3", "/root/cloner.py", token, admin_id], capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                await send_msg(context.bot, update.effective_chat.id, f"✅ <b>Clone Successful!</b>\n\n<pre>{res.stdout}</pre>")
            else:
                await send_msg(context.bot, update.effective_chat.id, f"❌ <b>Error:</b>\n<pre>{res.stderr}</pre>")
        except Exception as e:
            await reply_msg(update, f"❌ Error: {e}")
        context.user_data.pop("_manual_state", None)
        return True
    if state == "bot_name":
        new_name = text[:64]
        try:
            await context.bot.set_my_name(name=new_name)
            set_setting("bot_name", new_name)
            await reply_msg(update, f"✅ Bot name changed to: {new_name}")
        except Exception as e:
            await reply_msg(update, f"❌ Error: {e}")
        context.user_data.pop("_manual_state", None)
        return True
    if state == "bot_photo":
        if not msg.photo:
            await reply_msg(update, "❌ Please send a photo.")
            return True
        file_id = msg.photo[-1].file_id
        try:
            from telegram import InputFile
            import json
            tg_file = await context.bot.get_file(file_id)
            photo_bytes = bytes(await tg_file.download_as_bytearray())
            photo_file = InputFile(photo_bytes, filename="profile.jpg")
            # Telegram ko object chahiye, raw file nahi
            photo_obj = {
                "type": "static",
                "photo": "attach://profile.jpg",
            }
            await context.bot._post(
                "setMyProfilePhoto",
                data={
                    "photo": json.dumps(photo_obj),
                    "profile.jpg": photo_file,
                },
                read_timeout=60,
                write_timeout=60,
            )
            await reply_msg(update, "✅ Bot profile photo changed successfully!")
        except Exception as e:
            await reply_msg(update, f"❌ Error: {e}")
        context.user_data.pop("_manual_state", None)
        return True
    if state == "welcome_image":
        if not msg.photo:
            await reply_msg(update, "❌ Send a photo")
            return True
        set_setting("welcome_image", msg.photo[-1].file_id)
        context.user_data.pop("_manual_state", None)
        await reply_msg(update, "✅ Welcome image saved")
        return True
    if state == "welcome_videos":
        tmp = context.user_data.get("wv_tmp") or []
        if len(tmp) >= 5:
            await reply_msg(update, "ℹ️ Max 5 videos. Send /done")
            return True
        if msg.video:
            tmp.append(msg.video.file_id)
            context.user_data["wv_tmp"] = tmp
            await reply_msg(update, f"✅ Added video {len(tmp)}/5. Send /done to finish.")
            return True
        await reply_msg(update, "❌ Send a video")
        return True
    if state == "plan_photo":
        if not msg.photo:
            await reply_msg(update, "❌ Send a photo")
            return True
        set_setting("plan_photo", msg.photo[-1].file_id)
        context.user_data.pop("_manual_state", None)
        await reply_msg(update, "✅ Plan photo saved")
        return True
    if state == "demo_add":
        count = context.user_data.get("demo_count") or 0
        fid = None
        mtype = None
        emoji = "📎"
        if msg.video:
            fid = msg.video.file_id
            mtype = "video"
            emoji = "🎥"
        elif msg.photo:
            fid = msg.photo[-1].file_id
            mtype = "photo"
            emoji = "🖼"
        elif msg.animation:
            fid = msg.animation.file_id
            mtype = "animation"
            emoji = "🎞"
        elif msg.document:
            fid = msg.document.file_id
            mtype = "document"
            emoji = "📄"
        else:
            await reply_msg(
                update,
                "❌ Unsupported media.\n\n"
                "✅ Supported: photo, video, GIF (animation), document\n"
                "📤 Send media or /done to finish."
            )
            return True
        pid_sel = context.user_data.get("demo_plan_id", 0) or None
        con = db()
        try:
            con.execute(
                "INSERT INTO demo_media(media_type, file_id, sort_order, plan_id) VALUES(?, ?, ?, ?)",
                (mtype, fid, count, pid_sel),
            )
        finally:
            con.close()
        context.user_data["demo_count"] = count + 1
        tag = "🌐 Global" if pid_sel is None else f"💎 Plan #{pid_sel}"
        await reply_msg(
            update,
            f"✅ {emoji} {mtype.capitalize()} #{count + 1} added to {tag}.\n\n"
            f"📤 Send more or /done to finish."
        )
        return True
    if state == "broadcast_content":
        content = {}
        if msg.photo:
            content = {"type": "photo", "file_id": msg.photo[-1].file_id, "text": msg.caption or ""}
        elif msg.video:
            content = {"type": "video", "file_id": msg.video.file_id, "text": msg.caption or ""}
        elif msg.document:
            content = {"type": "document", "file_id": msg.document.file_id, "text": msg.caption or ""}
        elif msg.audio:
            content = {"type": "audio", "file_id": msg.audio.file_id, "text": msg.caption or ""}
        elif msg.voice:
            content = {"type": "voice", "file_id": msg.voice.file_id, "text": ""}
        elif msg.animation:
            content = {"type": "animation", "file_id": msg.animation.file_id, "text": msg.caption or ""}
        elif msg.sticker:
            content = {"type": "sticker", "file_id": msg.sticker.file_id, "text": ""}
        elif msg.text:
            content = {"type": "text", "text": msg.text}
        else:
            await reply_msg(update, "❌ Unsupported content")
            return True
        context.user_data["bc_content"] = content
        try:
            await context.bot.copy_message(update.effective_chat.id, update.effective_chat.id, msg.message_id)
        except TelegramError as e:
            logger.warning(f"preview failed: {e}")
        kb = InlineKeyboardMarkup([
            [btn("📤 Send Now", "admin:broadcast:send", style="success")],
            [btn("❌ Cancel", "admin:broadcast:cancel", style="danger")],
        ])
        await send_msg(context.bot, update.effective_chat.id, "📢 Preview above. Send now?", reply_markup=kb)
        return True
    if state == "reject_reason":
        pid = context.user_data.pop("reject_id", None)
        reason = text or "Not verified"
        context.user_data.pop("_manual_state", None)
        if pid is None:
            await reply_msg(update, "❌ No payment selected")
            return True
        payment = fetch_payment(pid)
        if payment is None:
            await reply_msg(update, "❌ Payment not found")
            return True
        if payment["status"] != "pending":
            await reply_msg(update, f"❌ Already {payment['status']}")
            return True
        con = db()
        try:
            con.execute("UPDATE payments SET status='rejected', rejection_reason=? WHERE id=?", (reason, pid))
        finally:
            con.close()
        try:
            await send_msg(
                context.bot, payment["user_id"],
                f"❌ Payment Rejected\n\n🧾 Payment ID: #{pid}\n📝 Reason: {esc(reason)}",
            )
        except TelegramError as e:
            logger.warning(f"reject notify failed: {e}")
        await reply_msg(update, "✅ Rejected and user notified")
        return True
    if state == "db_import":
        if not msg.document:
            await reply_msg(update, "❌ Send a ZIP or SQLite document")
            return True
        context.user_data.pop("_manual_state", None)
        await import_from_zip(update, context, msg.document)
        return True
    if state == "user_search":
        query = text.lstrip("@").strip()
        con = db()
        try:
            if query.isdigit():
                rows = con.execute("SELECT * FROM users WHERE id=?", (int(query),)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? LIMIT 10",
                    (f"%{query}%", f"%{query}%"),
                ).fetchall()
        finally:
            con.close()
        context.user_data.pop("_manual_state", None)
        if not rows:
            await reply_msg(update, "❌ Not found")
            return True
        now = now_iso()
        for r in rows:
            prem = r["premium_until"] and r["premium_until"] > now
            txt = (
                f"👤 {esc(r['first_name'])}\n"
                f"ID: <code>{r['id']}</code>\n"
                f"Username: @{esc(r['username'])}\n"
                f"Status: {esc(r['status'])}\n"
                f"Premium: {'✅' if prem else '❌'}\n"
                f"Expiry: {esc(r['premium_until'] or '-')}"
            )
            kb = InlineKeyboardMarkup([
                [btn("💎 +30 Days", f"admin:user:add30:{r['id']}", style="success"),
                 btn("❌ Remove", f"admin:user:remove:{r['id']}", style="danger")],
                [btn("🚫 Block" if (r["status"] or "active") == "active" else "✅ Unblock", f"admin:user:block:{r['id']}", style="primary")],
            ])
            await send_msg(context.bot, update.effective_chat.id, txt, reply_markup=kb)
        return True
    if state == "admin_add":
        try:
            uid = int(text)
        except ValueError:
            await reply_msg(update, "❌ Invalid ID")
            return True
        ids = set(current_admin_ids())
        ids.add(uid)
        extras = [str(x) for x in ids if x != ADMIN_ID]
        set_setting("extra_admins", ",".join(extras))
        context.user_data.pop("_manual_state", None)
        await reply_msg(update, f"✅ Admin added: {uid}")
        return True
    if state == "admin_remove":
        try:
            uid = int(text)
        except ValueError:
            await reply_msg(update, "❌ Invalid ID")
            return True
        if uid == ADMIN_ID:
            await reply_msg(update, "❌ Cannot remove primary admin")
            return True
        ids = set(current_admin_ids())
        ids.discard(uid)
        extras = [str(x) for x in ids if x != ADMIN_ID]
        set_setting("extra_admins", ",".join(extras))
        context.user_data.pop("_manual_state", None)
        await reply_msg(update, f"✅ Admin removed: {uid}")
        return True
    return False


async def message_router(update, context):
    if not update.message:
        return
    # Auto-delete last welcome media on any user action
    asyncio.create_task(_delete_welcome_media(context.bot, update.effective_chat.id))
    if context.user_data.get("verify_state"):
        try:
            handled = await verify_handle_message(update, context)
            if handled:
                return
        except Exception as e:
            logger.exception(f"verify handler error: {e}")
            return
    state = context.user_data.get("_manual_state")
    if admin_only(update) and state:
        try:
            if await handle_admin_state(update, context, state):
                return
        except Exception as e:
            logger.exception(f"admin state error: {e}")
            await reply_msg(update, f"❌ Error: {e}")
            return
    text = (update.message.text or "").strip()
    if text == "💎 GET PREMIUM":
        register_user(update.effective_user)
        await send_start_screen(context.bot, update.effective_chat.id)
        return
    if text == "🥵 FREE DEMO":
        con = db()
        try:
            rows = con.execute("SELECT * FROM demo_media ORDER BY sort_order ASC, id ASC").fetchall()
        finally:
            con.close()
        if not rows:
            await reply_msg(update, "😕 No demos available")
            return
        protect = get_setting("protect_content", "0") == "1"
        for r in rows:
            try:
                if r["media_type"] == "video":
                    await context.bot.send_video(update.effective_chat.id, r["file_id"], has_spoiler=True, protect_content=protect)
                elif r["media_type"] == "photo":
                    await context.bot.send_photo(update.effective_chat.id, r["file_id"], has_spoiler=True, protect_content=protect)
                elif r["media_type"] == "animation":
                    await context.bot.send_animation(update.effective_chat.id, r["file_id"], has_spoiler=True, protect_content=protect)
                else:
                    await context.bot.send_document(update.effective_chat.id, r["file_id"], protect_content=protect)
            except TelegramError as e:
                logger.warning(f"demo send fail: {e}")
        return
    if update.effective_user:
        register_user(update.effective_user)


async def post_init(app):
    init_db()
    log_event("Bot started")
    try:
        me = await app.bot.get_me()
        logger.info(f"Bot started as @{me.username} ({me.id})")
    except TelegramError as e:
        logger.warning(f"get_me failed: {e}")


def build_app():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .connection_pool_size(256)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router))
    return app


if __name__ == "__main__":
    init_db()
    build_app().run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=0.5,
        timeout=30,
    )
