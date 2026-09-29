import hashlib
import hmac
import html
import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CENTER_CHAT_ID = os.environ["CENTER_CHAT_ID"].strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ["ADMIN_IDS"].split(",")
    if x.strip()
}

TELEGRAM_WEBHOOK_SECRET = os.environ[
    "TELEGRAM_WEBHOOK_SECRET"
].strip()

EMAIL_INGEST_SECRET = os.environ[
    "EMAIL_INGEST_SECRET"
].strip()

WHATSAPP_GATEWAY_SECRET = os.environ[
    "WHATSAPP_GATEWAY_SECRET"
].strip()

# Meta variables are kept because the old Meta webhook route still exists.
WA_VERIFY_TOKEN = os.getenv(
    "WHATSAPP_VERIFY_TOKEN",
    ""
).strip()

WA_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN",
    ""
).strip()

WA_GRAPH_VERSION = os.getenv(
    "WHATSAPP_GRAPH_VERSION",
    ""
).strip()

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024


# ============================================================
# LOGGING
# ============================================================

def log(*args):
    print(*args, flush=True)


# ============================================================
# TELEGRAM
# ============================================================

def tg(
    method,
    payload=None,
    files=None,
    timeout=90
):
    payload = payload or {}

    url = f"{TG_API}/{method}"

    log(
        "========== TELEGRAM DEBUG =========="
    )

    log(
        "METHOD:",
        method
    )

    log(
        "URL:",
        url
    )

    try:
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

    except Exception as exc:
        log(
            "Telegram request exception:",
            repr(exc)
        )
        raise

    log(
        "STATUS:",
        response.status_code
    )

    log(
        "RESPONSE:",
        response.text
    )

    log(
        "===================================="
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result)

    return result["result"]


# ============================================================
# HELPERS
# ============================================================

def safe_filename(filename):
    filename = Path(
        filename or "course.bin"
    ).name

    filename = re.sub(
        r"[^\w.\-() ]+",
        "_",
        filename,
        flags=re.UNICODE
    )

    filename = filename.strip()

    return (
        filename[:180]
        or "course.bin"
    )


def escape_html(value):
    return html.escape(
        str(value or ""),
        quote=False
    )


def check_secret(
    header_name,
    expected
):
    supplied = request.headers.get(
        header_name,
        ""
    )

    return (
        bool(supplied)
        and hmac.compare_digest(
            supplied,
            expected
        )
    )


def build_review_caption(
    source,
    sender,
    filename,
    caption,
    digest
):
    return (
        "📥 <b>طلب كور جديد</b>\n\n"
        f"📌 المصدر: {escape_html(source)}\n"
        f"👤 المرسل: {escape_html(sender)}\n"
        f"📄 الملف: {escape_html(filename)}\n"
        f"📝 الملاحظة: {escape_html(caption or '—')}\n"
        f"🔐 SHA256: {escape_html(digest[:16])}…\n\n"
        "الحالة: ⏳ بانتظار المراجعة"
    )


# ============================================================
# SEND TO ADMINS
# ============================================================

def send_submission_to_admins(
    source,
    sender,
    filename,
    data,
    caption
):
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(
            "الملف أكبر من 50 MB."
        )

    clean_name = safe_filename(
        filename
    )

    digest = hashlib.sha256(
        data
    ).hexdigest()

    review_caption = build_review_caption(
        source=source,
        sender=sender,
        filename=clean_name,
        caption=caption,
        digest=digest
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

    if not ADMIN_IDS:
        raise RuntimeError(
            "ADMIN_IDS is empty."
        )

    first_message_id = None

    for admin_id in ADMIN_IDS:

        log(
            "========== SEND COURSE DEBUG =========="
        )

        log(
            "ADMIN_ID:",
            admin_id
        )

        log(
            "FILENAME:",
            clean_name
        )

        log(
            "SIZE:",
            len(data)
        )

        log(
            "SOURCE:",
            source
        )

        log(
            "SENDER:",
            sender
        )

        log(
            "======================================="
        )

        result = tg(
            "sendDocument",
            payload={
                "chat_id": admin_id,
                "caption": review_caption[:1024],
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

        if first_message_id is None:
            first_message_id = (
                result["message_id"]
            )

    return first_message_id


# ============================================================
# APPROVE / REJECT
# ============================================================

def approve_from_admin_message(
    message
):
    chat_id = message[
        "chat"
    ][
        "id"
    ]

    if not message.get(
        "document"
    ):
        raise ValueError(
            "الرسالة لا تحتوي على document."
        )

    sent = tg(
        "copyMessage",
        {
            "chat_id": CENTER_CHAT_ID,
            "from_chat_id": chat_id,
            "message_id": message[
                "message_id"
            ]
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
            "message_id": message[
                "message_id"
            ],
            "caption": approved_caption[
                :1024
            ],
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": []
            }
        }
    )

    return sent


def reject_admin_message(
    message
):
    chat_id = message[
        "chat"
    ][
        "id"
    ]

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
            "message_id": message[
                "message_id"
            ],
            "caption": rejected_caption[
                :1024
            ],
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": []
            }
        }
    )


# ============================================================
# BASIC
# ============================================================

@app.get("/")
def index():
    return (
        "Course Center Bot is alive.",
        200
    )


@app.get("/health")
def health():

    return jsonify({
        "ok": True,
        "admin_count": len(
            ADMIN_IDS
        ),
        "center_chat_configured": bool(
            CENTER_CHAT_ID
        ),
        "whatsapp_gateway_configured": bool(
            WHATSAPP_GATEWAY_SECRET
        ),
        "whatsapp_meta_configured": bool(
            WA_VERIFY_TOKEN
            and WA_ACCESS_TOKEN
            and WA_GRAPH_VERSION
        )
    })


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/webhook/telegram")
def telegram_webhook():

    if not check_secret(
        "X-Telegram-Bot-Api-Secret-Token",
        TELEGRAM_WEBHOOK_SECRET
    ):
        log(
            "Telegram webhook: invalid secret."
        )

        return (
            "forbidden",
            403
        )

    update = request.get_json(
        silent=True
    ) or {}

    callback = update.get(
        "callback_query"
    )

    if not callback:
        return jsonify({
            "ok": True
        })

    admin_id = callback.get(
        "from",
        {}
    ).get(
        "id"
    )

    log(
        "Telegram callback from admin:",
        admin_id
    )

    if admin_id not in ADMIN_IDS:

        try:
            tg(
                "answerCallbackQuery",
                {
                    "callback_query_id":
                        callback["id"],
                    "text":
                        "غير مسموح."
                }
            )

        except Exception as exc:
            log(
                "answerCallbackQuery error:",
                repr(exc)
            )

        return jsonify({
            "ok": True
        })

    action = callback.get(
        "data"
    )

    message = callback.get(
        "message"
    )

    if not message:
        return jsonify({
            "ok": True
        })

    try:

        if action == "approve":

            result = (
                approve_from_admin_message(
                    message
                )
            )

            tg(
                "answerCallbackQuery",
                {
                    "callback_query_id":
                        callback["id"],
                    "text":
                        "تم النشر في السونتر ✅"
                }
            )

            return jsonify({
                "ok": True,
                "center_message_id":
                    result.get(
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
                    "callback_query_id":
                        callback["id"],
                    "text":
                        "تم الرفض ❌"
                }
            )

            return jsonify({
                "ok": True
            })

    except Exception as exc:

        log(
            "Telegram callback error:",
            repr(exc)
        )

        try:
            tg(
                "answerCallbackQuery",
                {
                    "callback_query_id":
                        callback["id"],
                    "text":
                        "حدث خطأ، راجع اللوج."
                }
            )

        except Exception as answer_exc:

            log(
                "Callback error notification failed:",
                repr(answer_exc)
            )

        return jsonify({
            "ok": False,
            "error": str(exc)
        })

    return jsonify({
        "ok": True
    })


# ============================================================
# EMAIL INGEST
# ============================================================

@app.post("/ingest/email")
def ingest_email():

    if not check_secret(
        "X-Email-Ingest-Secret",
        EMAIL_INGEST_SECRET
    ):
        log(
            "Email ingest: invalid secret."
        )

        return (
            "forbidden",
            403
        )

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

        log(
            "Email ingest: file missing."
        )

        return jsonify({
            "ok": False,
            "error": "file missing"
        }), 400

    data = uploaded.read()

    log(
        "========== EMAIL INGEST DEBUG =========="
    )

    log(
        "SENDER:",
        sender
    )

    log(
        "SUBJECT:",
        subject
    )

    log(
        "FILE:",
        filename
    )

    log(
        "BYTES:",
        len(data)
    )

    log(
        "ADMIN_IDS:",
        ADMIN_IDS
    )

    log(
        "========================================"
    )

    try:

        message_id = send_submission_to_admins(
            source="email",
            sender=sender,
            filename=filename,
            data=data,
            caption=subject
        )

        log(
            "Email ingest success. "
            "Admin message ID:",
            message_id
        )

        return jsonify({
            "ok": True,
            "admin_message_id":
                message_id
        })

    except Exception as exc:

        log(
            "Email ingest error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# WHATSAPP GATEWAY INGEST
# ============================================================

@app.post("/ingest/whatsapp")
def ingest_whatsapp_gateway():

    if not check_secret(
        "X-WhatsApp-Gateway-Secret",
        WHATSAPP_GATEWAY_SECRET
    ):
        log(
            "WhatsApp gateway ingest: invalid secret."
        )

        return (
            "forbidden",
            403
        )

    sender = request.form.get(
        "sender",
        "unknown"
    )

    caption = request.form.get(
        "caption",
        ""
    )

    filename = request.form.get(
        "filename",
        "whatsapp_file.bin"
    )

    uploaded = request.files.get(
        "file"
    )

    log(
        "========== WHATSAPP GATEWAY INGEST =========="
    )

    log(
        "SENDER:",
        sender
    )

    log(
        "CAPTION:",
        caption
    )

    log(
        "FILENAME:",
        filename
    )

    log(
        "HAS_FILE:",
        uploaded is not None
    )

    log(
        "============================================="
    )

    if uploaded is None:

        return jsonify({
            "ok": False,
            "error": "file missing"
        }), 400

    data = uploaded.read()

    log(
        "WHATSAPP FILE BYTES:",
        len(data)
    )

    try:

        message_id = send_submission_to_admins(
            source="whatsapp",
            sender=sender,
            filename=filename,
            data=data,
            caption=caption
        )

        log(
            "WhatsApp gateway ingest success. "
            "Admin message ID:",
            message_id
        )

        return jsonify({
            "ok": True,
            "admin_message_id":
                message_id
        })

    except Exception as exc:

        log(
            "WhatsApp gateway ingest error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# META WHATSAPP HELPERS
# ============================================================

def whatsapp_media(
    media_id
):
    if not (
        WA_ACCESS_TOKEN
        and WA_GRAPH_VERSION
    ):
        raise RuntimeError(
            "WhatsApp is not configured."
        )

    url = (
        f"https://graph.facebook.com/"
        f"{WA_GRAPH_VERSION}/"
        f"{media_id}"
    )

    headers = {
        "Authorization":
            f"Bearer {WA_ACCESS_TOKEN}"
    }

    meta = requests.get(
        url,
        headers=headers,
        timeout=30
    )

    meta.raise_for_status()

    info = meta.json()

    media_url = info.get(
        "url"
    )

    if not media_url:
        raise RuntimeError(
            "WhatsApp media URL was not returned."
        )

    mime_type = info.get(
        "mime_type",
        "application/octet-stream"
    )

    data_response = requests.get(
        media_url,
        headers=headers,
        timeout=120
    )

    data_response.raise_for_status()

    return (
        data_response.content,
        mime_type
    )


def default_filename(
    mime_type,
    kind
):
    extensions = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    }

    return (
        f"whatsapp_{kind}"
        f"{extensions.get(mime_type, '')}"
    )


# ============================================================
# META WHATSAPP WEBHOOK
# ============================================================

@app.route(
    "/webhook/whatsapp",
    methods=["GET", "POST"]
)
def whatsapp_webhook():

    if request.method == "GET":

        if (
            request.args.get(
                "hub.mode"
            ) == "subscribe"

            and

            request.args.get(
                "hub.verify_token"
            ) == WA_VERIFY_TOKEN
        ):

            return request.args.get(
                "hub.challenge",
                ""
            ), 200

        return (
            "forbidden",
            403
        )

    payload = request.get_json(
        silent=True
    ) or {}

    try:

        for entry in payload.get(
            "entry",
            []
        ):

            for change in entry.get(
                "changes",
                []
            ):

                value = change.get(
                    "value",
                    {}
                )

                for message in value.get(
                    "messages",
                    []
                ):

                    sender = message.get(
                        "from",
                        "unknown"
                    )

                    kind = message.get(
                        "type"
                    )

                    if kind not in {
                        "document",
                        "image"
                    }:
                        continue

                    media = message.get(
                        kind,
                        {}
                    )

                    media_id = media.get(
                        "id"
                    )

                    if not media_id:
                        continue

                    data, mime_type = (
                        whatsapp_media(
                            media_id
                        )
                    )

                    filename = (
                        media.get("filename")
                        or default_filename(
                            mime_type,
                            kind
                        )
                    )

                    caption = media.get(
                        "caption",
                        ""
                    )

                    send_submission_to_admins(
                        source="whatsapp",
                        sender=sender,
                        filename=filename,
                        data=data,
                        caption=caption
                    )

        return jsonify({
            "ok": True
        })

    except Exception as exc:

        log(
            "WhatsApp webhook error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# MAIN
# ============================================================


@app.post("/cron/email")
def trigger_email_workflow():
    cron_secret = os.environ[
        "EMAIL_CRON_SECRET"
    ].strip()

    if not check_secret(
        "X-Cron-Secret",
        cron_secret
    ):
        log(
            "Email cron: invalid secret."
        )

        return (
            "forbidden",
            403
        )

    github_token = os.environ[
        "GITHUB_ACTIONS_TOKEN"
    ].strip()

    url = (
        "https://api.github.com/repos/"
        "MahdiBoumaaraf/HealyaBot/"
        "actions/workflows/email.yml/dispatches"
    )

    response = requests.post(
        url,
        headers={
            "Accept":
                "application/vnd.github+json",
            "Authorization":
                f"Bearer {github_token}",
            "X-GitHub-Api-Version":
                "2026-03-10",
            "Content-Type":
                "application/json",
        },
        json={
            "ref": "main"
        },
        timeout=30
    )

    log(
        "GitHub workflow dispatch:",
        response.status_code,
        response.text
    )

    if response.status_code not in (200, 204):
        return jsonify({
            "ok": False,
            "github_status":
                response.status_code,
            "error":
                response.text
        }), 502

    return jsonify({
        "ok": True,
        "message":
            "Email workflow dispatched."
    })


if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    log(
        "Starting Course Center Bot..."
    )

    log(
        "Admin IDs:",
        ADMIN_IDS
    )

    log(
        "Center chat configured:",
        bool(CENTER_CHAT_ID)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
