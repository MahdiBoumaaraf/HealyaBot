import hashlib
import hmac
import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CENTER_CHAT_ID = os.environ["CENTER_CHAT_ID"]
ADMIN_IDS = {
    int(x.strip())
    for x in os.environ["ADMIN_IDS"].split(",")
    if x.strip()
}

TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
EMAIL_INGEST_SECRET = os.environ["EMAIL_INGEST_SECRET"]

WA_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WA_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024


def tg(method, payload=None, files=None, timeout=90):
    payload = payload or {}
    url = f"{TG_API}/{method}"

    if files:
        response = requests.post(
            url,
            data=payload,
            files=files,
            timeout=timeout
        )
    else:
        response = requests.post(
            url,
            json=payload,
            timeout=timeout
        )

    print("========== TELEGRAM DEBUG ==========")
    print("URL:", url)
    print("STATUS:", response.status_code)
    print("RESPONSE:", response.text)
    print("====================================")

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result)

    return result["result"]


def safe_filename(filename):
    filename = Path(filename or "course.bin").name
    filename = re.sub(
        r"[^\w.\-() ]+",
        "_",
        filename,
        flags=re.UNICODE
    )

    return filename[:180] or "course.bin"


def check_secret(header_name, expected):
    supplied = request.headers.get(header_name, "")
    return bool(supplied) and hmac.compare_digest(
        supplied,
        expected
    )


def send_submission_to_admins(
    source,
    sender,
    filename,
    data,
    caption,
):

    if len(data) > MAX_FILE_BYTES:
        raise ValueError(
            "الملف أكبر من 50 MB."
        )

    clean_name = safe_filename(filename)

    digest = hashlib.sha256(data).hexdigest()

    review_text = (
        "📥 <b>طلب كور جديد</b>\n\n"
        f"📌 المصدر: {source}\n"
        f"👤 المرسل: {sender}\n"
        f"📄 الملف: {clean_name}\n"
        f"📝 الملاحظة: {caption or '—'}\n"
        f"🔐 SHA256: {digest[:16]}…"
    )


    keyboard = {
        "inline_keyboard": [[
            {
                "text": "✅ قبول ونشر",
                "callback_data": "approve"
            },
            {
                "text": "❌ رفض",
                "callback_data": "reject"
            }
        ]]
    }


    for admin_id in ADMIN_IDS:

        print("========== EMAIL TO TELEGRAM DEBUG ==========")
        print("ADMIN ID:", admin_id)
        print("FILE:", clean_name)
        print("SIZE:", len(data))
        print("============================================")


        result = tg(
            "sendDocument",
            payload={
                "chat_id": admin_id,
                "caption": (
                    f"{review_text}\n\n"
                    "الحالة: ⏳ بانتظار المراجعة"
                ),
                "parse_mode": "HTML",
                "reply_markup": json.dumps(
                    keyboard,
                    ensure_ascii=False
                )
            },
            files={
                "document": (
                    clean_name,
                    data,
                    "application/octet-stream"
                )
            }
        )

        return result["message_id"]
        def approve_from_admin_message(message):

    chat_id = message["chat"]["id"]

    document = message.get("document")

    if not document:
        raise ValueError(
            "الرسالة لا تحتوي على document."
        )


    sent = tg(
        "copyMessage",
        {
            "chat_id": CENTER_CHAT_ID,
            "from_chat_id": chat_id,
            "message_id": message["message_id"]
        }
    )


    old_caption = message.get(
        "caption",
        ""
    )

    approved_caption = (
        old_caption
        + "\n\n✅ الحالة: تم النشر في السونتر"
    )


    tg(
        "editMessageCaption",
        {
            "chat_id": chat_id,
            "message_id": message["message_id"],
            "caption": approved_caption[:1024],
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": []
            }
        }
    )


    return sent



def reject_admin_message(message):

    chat_id = message["chat"]["id"]

    old_caption = message.get(
        "caption",
        ""
    )


    rejected_caption = (
        old_caption
        + "\n\n❌ الحالة: مرفوض"
    )


    tg(
        "editMessageCaption",
        {
            "chat_id": chat_id,
            "message_id": message["message_id"],
            "caption": rejected_caption[:1024],
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": []
            }
        }
    )



@app.get("/")
def index():
    return "Course Center Bot is alive.", 200



@app.get("/health")
def health():
    return jsonify({
        "ok": True
    })



@app.post("/webhook/telegram")
def telegram_webhook():

    if not check_secret(
        "X-Telegram-Bot-Api-Secret-Token",
        TELEGRAM_WEBHOOK_SECRET
    ):
        return "forbidden", 403


    update = request.get_json(
        silent=True
    ) or {}


    cq = update.get(
        "callback_query"
    )


    if not cq:
        return jsonify({
            "ok": True
        })


    admin_id = cq.get(
        "from",
        {}
    ).get(
        "id"
    )


    if admin_id not in ADMIN_IDS:

        tg(
            "answerCallbackQuery",
            {
                "callback_query_id": cq["id"],
                "text": "غير مسموح."
            }
        )

        return jsonify({
            "ok": True
        })


    action = cq.get(
        "data"
    )

    message = cq.get(
        "message"
    )


    if not message:
        return jsonify({
            "ok": True
        })


    try:

        if action == "approve":

            result = approve_from_admin_message(
                message
            )


            tg(
                "answerCallbackQuery",
                {
                    "callback_query_id": cq["id"],
                    "text": "تم النشر في السونتر ✅"
                }
            )


            return jsonify({
                "ok": True,
                "center_message_id": result.get(
                    "message_id"
                )
            })


        if action == "reject":

            reject_admin_message(
                message
            )


            tg(
                "answerCallbackQuery",
                {
                    "callback_query_id": cq["id"],
                    "text": "تم الرفض ❌"
                }
            )


            return jsonify({
                "ok": True
            })


    except Exception as exc:

        print(
            "Telegram callback error:",
            repr(exc)
        )


    return jsonify({
        "ok": True
    })



@app.post("/ingest/email")
def ingest_email():

    if not check_secret(
        "X-Email-Ingest-Secret",
        EMAIL_INGEST_SECRET
    ):
        return "forbidden", 403


    sender = request.form.get(
        "sender",
        "unknown"
    )

    subject = request.form.get(
        "subject",
        ""
    )

    filename = request.form.get(
        "filename",
        "course.bin"
    )


    uploaded = request.files.get(
        "file"
    )


    if uploaded is None:

        return jsonify({
            "ok": False,
            "error": "file missing"
        }), 400


    data = uploaded.read()


    print("========== EMAIL INGEST DEBUG ==========")
    print("SENDER:", sender)
    print("SUBJECT:", subject)
    print("FILE:", filename)
    print("BYTES:", len(data))
    print("========================================")


    try:

        message_id = send_submission_to_admins(
            source="email",
            sender=sender,
            filename=filename,
            data=data,
            caption=subject
        )


        return jsonify({
            "ok": True,
            "admin_message_id": message_id
        })


    except Exception as exc:

        print(
            "Email ingest error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500
