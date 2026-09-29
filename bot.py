import os
import re
import asyncio
import hashlib
from collections import defaultdict

import aiohttp
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@KhanMMStore")

DISCORD_WEBHOOK_K1200 = os.getenv("DISCORD_WEBHOOK_K1200")
DISCORD_WEBHOOK_K1800 = os.getenv("DISCORD_WEBHOOK_K1800")
DISCORD_WEBHOOK_K1800_PLUS = os.getenv("DISCORD_WEBHOOK_K1800_PLUS")

KHAN_WHATSAPP = "+92 339 1437905"
KHAN_TELEGRAM = "@khanlmshop"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

# =========================================================
# STATE
# =========================================================

albums = {}
album_tasks = {}

processed_messages = set()
processed_fingerprints = set()

paused = False

stats = {
    "received": 0,
    "posted": 0,
    "failed": 0,
}

# =========================================================
# PRICE SYSTEM
# =========================================================

def get_markup(price):
    if price <= 50:
        return 5
    elif price <= 100:
        return 10
    elif price <= 150:
        return 15
    elif price <= 200:
        return 20
    elif price <= 250:
        return 25
    elif price <= 299:
        return 30
    elif price <= 400:
        return 35
    elif price <= 499:
        return 40
    elif price <= 2000:
        return 50
    elif price <= 4999:
        return 100
    elif price <= 9999:
        return 110
    else:
        return 130


def find_price(text):
    candidates = []

    dollar_patterns = [
        r'\$\s*(\d+(?:\.\d+)?)\s*([kKmM])?',
        r'(\d+(?:\.\d+)?)\s*([kKmM])?\s*\$',
    ]

    for pattern in dollar_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            number = float(match.group(1))
            suffix = match.group(2)

            if suffix:
                if suffix.lower() == "k":
                    number *= 1000
                elif suffix.lower() == "m":
                    number *= 1000000

            candidates.append(number)

    mm_pattern = r'(\d+(?:\.\d+)?)\s*([kKmM])?\s*\+\s*MM\b'

    for match in re.finditer(mm_pattern, text, re.IGNORECASE):
        number = float(match.group(1))
        suffix = match.group(2)

        if suffix:
            if suffix.lower() == "k":
                number *= 1000
            elif suffix.lower() == "m":
                number *= 1000000

        candidates.append(number)

    return candidates[-1] if candidates else None


def remove_price_tokens(text):
    patterns = [
        r'\$\s*\d+(?:\.\d+)?\s*[kKmM]?',
        r'\d+(?:\.\d+)?\s*[kKmM]?\s*\$',
        r'\d+(?:\.\d+)?\s*[kKmM]?\s*\+\s*MM\b',
    ]

    for pattern in patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    return text


# =========================================================
# CONTACT CLEANING
# =========================================================

def clean_listing(text):
    lines = text.splitlines()
    cleaned = []

    contact_words = (
        "contact",
        "whatsapp",
        "telegram",
        "telegram id",
        "tg",
        "wa",
        "line",
        "discord",
        "wechat",
        "email",
        "e-mail",
    )

    for line in lines:
        original = line
        lower = line.lower().strip()

        # Remove lines that clearly contain seller contact information
        if any(word in lower for word in contact_words):
            continue

        # Remove URLs
        line = re.sub(
            r'https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|wa\.me/\S+|discord\.gg/\S+',
            '',
            line,
            flags=re.IGNORECASE
        )

        # Remove @usernames
        line = re.sub(r'@\w+', '', line)

        # Remove phone numbers ONLY on obvious phone/contact lines
        if any(word in lower for word in (
            "phone", "mobile", "whatsapp", "contact"
        )):
            line = re.sub(r'\+?\d[\d\s().-]{7,}\d', '', line)

        line = line.strip()

        if line:
            cleaned.append(line)

    return "\n".join(cleaned).strip()


# =========================================================
# FORMAT LISTING
# =========================================================

def format_listing(text):
    original_price = find_price(text)

    if original_price is None:
        return None

    markup = get_markup(original_price)
    final_price = original_price + markup

    text = remove_price_tokens(text)
    text = clean_listing(text)

    if final_price.is_integer():
        price_text = str(int(final_price))
    else:
        price_text = f"{final_price:.2f}".rstrip("0").rstrip(".")

    result = text.strip()

    if result:
        result += "\n\n"

    result += f"${price_text} + MM\n\n"
    result += "Contact KHAN SHOP:\n"
    result += f"WhatsApp: {KHAN_WHATSAPP}\n"
    result += f"Telegram: {KHAN_TELEGRAM}"

    return result


# =========================================================
# DISCORD KINGDOM ROUTING
# =========================================================

def get_discord_webhook(text):

    # Can Fly to 1850 / Can go to 1850 / Fly to 1850
    patterns = [
        r'(?:can\s+)?(?:fly|go)\s+to\s+(?:kingdom\s*)?(\d{1,4})',
        r'kingdom\s*(\d{1,4})',
        r'\bK(?:D)?\s*(\d{1,4})\b',
    ]

    kingdom = None

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            kingdom = int(match.group(1))
            break

    if kingdom is None:
        return DISCORD_WEBHOOK_K1200

    if kingdom <= 1200:
        return DISCORD_WEBHOOK_K1200

    if kingdom <= 1800:
        return DISCORD_WEBHOOK_K1800

    return DISCORD_WEBHOOK_K1800_PLUS


# =========================================================
# DISCORD POST
# =========================================================

async def post_to_discord(photo_ids, caption, original_text, context):
    webhook = get_discord_webhook(original_text)

    if not webhook:
        raise RuntimeError("Discord webhook is not configured.")

    async with aiohttp.ClientSession() as session:

        form = aiohttp.FormData()

        form.add_field(
            "payload_json",
            '{"content": ' + json_string(caption) + '}'
        )

        for index, photo_id in enumerate(photo_ids[:10]):

            tg_file = await context.bot.get_file(photo_id)
            file_bytes = await tg_file.download_as_bytearray()

            form.add_field(
                f"files[{index}]",
                bytes(file_bytes),
                filename=f"listing_{index + 1}.jpg",
                content_type="image/jpeg",
            )

        async with session.post(webhook, data=form) as response:

            if response.status >= 300:
                body = await response.text()
                raise RuntimeError(
                    f"Discord HTTP {response.status}: {body[:500]}"
                )


def json_string(value):
    import json
    return json.dumps(value, ensure_ascii=False)


# =========================================================
# TELEGRAM CHANNEL POST
# =========================================================

async def post_to_khan_channel(photo_ids, caption, context):

    if not photo_ids:
        return

    if len(photo_ids) == 1:

        await context.bot.send_photo(
            chat_id=TELEGRAM_CHANNEL,
            photo=photo_ids[0],
            caption=caption
        )

    else:

        media = []

        for index, photo_id in enumerate(photo_ids[:10]):

            if index == 0:
                media.append(
                    InputMediaPhoto(
                        media=photo_id,
                        caption=caption
                    )
                )
            else:
                media.append(
                    InputMediaPhoto(media=photo_id)
                )

        await context.bot.send_media_group(
            chat_id=TELEGRAM_CHANNEL,
            media=media
        )


# =========================================================
# RETRY
# =========================================================

async def retry(coro_factory, attempts=3, delay=3):

    last_error = None

    for attempt in range(attempts):

        try:
            return await coro_factory()

        except Exception as e:
            last_error = e

            if attempt < attempts - 1:
                await asyncio.sleep(delay)

    raise last_error


# =========================================================
# PUBLISH EVERYWHERE
# =========================================================

async def publish_everywhere(
    photo_ids,
    processed_caption,
    original_caption,
    context
):

    global stats

    try:

        await retry(
            lambda: post_to_khan_channel(
                photo_ids,
                processed_caption,
                context
            )
        )

        if (
            DISCORD_WEBHOOK_K1200
            or DISCORD_WEBHOOK_K1800
            or DISCORD_WEBHOOK_K1800_PLUS
        ):
            await retry(
                lambda: post_to_discord(
                    photo_ids,
                    processed_caption,
                    original_caption,
                    context
                )
            )

        stats["posted"] += 1

    except Exception as e:

        stats["failed"] += 1

        print("PUBLISH ERROR:", repr(e))


# =========================================================
# DUPLICATE PROTECTION
# =========================================================

def make_fingerprint(text, photo_ids):

    raw = text.strip() + "|" + "|".join(photo_ids)

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# =========================================================
# ALBUM PROCESSOR
# =========================================================

async def process_album(group_id, context):

    await asyncio.sleep(7)

    data = albums.pop(group_id, None)

    if not data:
        return

    album_tasks.pop(group_id, None)

    photos = data["photos"]
    original_caption = data["caption"]

    fingerprint = make_fingerprint(
        original_caption,
        photos
    )

    if fingerprint in processed_fingerprints:
        print("Duplicate album ignored.")
        return

    processed_fingerprints.add(fingerprint)

    processed_caption = format_listing(original_caption)

    if not processed_caption:
        print("No valid price found in album.")
        return

    await publish_everywhere(
        photos,
        processed_caption,
        original_caption,
        context
    )


# =========================================================
# ALBUM HANDLER
# =========================================================

async def handle_album(message, context):

    group_id = message.media_group_id

    if group_id not in albums:

        albums[group_id] = {
            "photos": [],
            "caption": "",
        }

    data = albums[group_id]

    photo_id = message.photo[-1].file_id

    if photo_id not in data["photos"]:
        data["photos"].append(photo_id)

    if message.caption:
        data["caption"] = message.caption

    if group_id not in album_tasks:

        album_tasks[group_id] = asyncio.create_task(
            process_album(group_id, context)
        )


# =========================================================
# PHOTO HANDLER
# =========================================================

async def handle_photo(update, context):

    global paused

    if paused:
        return

    message = update.effective_message

    if not message:
        return

    stats["received"] += 1

    if message.media_group_id:
        await handle_album(message, context)
        return

    photo_id = message.photo[-1].file_id
    original_caption = message.caption or ""

    fingerprint = make_fingerprint(
        original_caption,
        [photo_id]
    )

    if fingerprint in processed_fingerprints:
        return

    processed_fingerprints.add(fingerprint)

    processed_caption = format_listing(original_caption)

    if not processed_caption:
        await message.reply_text(
            "⚠️ No valid price detected. Listing was not posted."
        )
        return

    await message.reply_text(
        processed_caption
    )

    await publish_everywhere(
        [photo_id],
        processed_caption,
        original_caption,
        context
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def handle_text(update, context):

    global paused

    if paused:
        return

    message = update.effective_message

    if not message or not message.text:
        return

    stats["received"] += 1

    original_text = message.text

    fingerprint = make_fingerprint(
        original_text,
        []
    )

    if fingerprint in processed_fingerprints:
        return

    processed_fingerprints.add(fingerprint)

    processed_caption = format_listing(original_text)

    if not processed_caption:
        await message.reply_text(
            "⚠️ No valid price detected. Listing was not posted."
        )
        return

    await message.reply_text(
        processed_caption
    )

    await publish_everywhere(
        [],
        processed_caption,
        original_text,
        context
    )


# =========================================================
# COMMANDS
# =========================================================

async def cmd_start(update, context):
    await update.message.reply_text(
        "KHAN SHOP AutoBot is online ✅"
    )


async def cmd_ping(update, context):
    await update.message.reply_text(
        "Pong 🟢"
    )


async def cmd_status(update, context):

    state = "PAUSED 🟡" if paused else "RUNNING 🟢"

    await update.message.reply_text(
        f"KHAN SHOP AutoBot\n\n"
        f"Status: {state}\n"
        f"Received: {stats['received']}\n"
        f"Posted: {stats['posted']}\n"
        f"Failed: {stats['failed']}"
    )


async def cmd_pause(update, context):

    global paused
    paused = True

    await update.message.reply_text(
        "Auto-posting paused 🛑"
    )


async def cmd_resume(update, context):

    global paused
    paused = False

    await update.message.reply_text(
        "Auto-posting resumed 🟢"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", cmd_start)
    )

    app.add_handler(
        CommandHandler("ping", cmd_ping)
    )

    app.add_handler(
        CommandHandler("status", cmd_status)
    )

    app.add_handler(
        CommandHandler("pause", cmd_pause)
    )

    app.add_handler(
        CommandHandler("resume", cmd_resume)
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    print("KHAN SHOP AutoBot starting...")
    print("Telegram channel:", TELEGRAM_CHANNEL)

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
