import hashlib
import hmac
import html
import json
import os
import re
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CENTER_CHAT_ID = os.getenv("CENTER_CHAT_ID", "").strip()
DATABASE_URL = os.environ["DATABASE_URL"].strip()

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

EMAIL_CRON_SECRET = os.environ.get(
    "EMAIL_CRON_SECRET",
    ""
).strip()

GITHUB_ACTIONS_TOKEN = os.environ.get(
    "GITHUB_ACTIONS_TOKEN",
    ""
).strip()

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TITLE_LENGTH = 200


# ============================================================
# DESTINATIONS / TOPICS
# ============================================================


def load_destinations():
    """
    CENTER_CHAT_ID format:

        group_id_topic_id_topic_id/group_id_topic_id_topic_id

    Example:

        -1001111111111_1_2/-1002222222222_2_4

    A group without topics can be written as just its group id:

        -1003333333333
    """
    raw = CENTER_CHAT_ID.strip()

    if not raw:
        raise RuntimeError(
            "CENTER_CHAT_ID is required."
        )

    destinations = {}

    for index, group_spec in enumerate(
        raw.split("/"),
        start=1,
    ):
        group_spec = group_spec.strip()

        if not group_spec:
            raise RuntimeError(
                "CENTER_CHAT_ID contains an empty group entry."
            )

        parts = group_spec.split("_")
        group_id_raw = parts[0].strip()
        topic_ids_raw = [
            item.strip()
            for item in parts[1:]
            if item.strip()
        ]

        if not group_id_raw.lstrip("-").isdigit():
            raise RuntimeError(
                f"Invalid group id in CENTER_CHAT_ID: {group_id_raw}"
            )

        chat_id = int(group_id_raw)
        destination_key = str(index)

        topic_map = {}
        for topic_id_raw in topic_ids_raw:
            if not topic_id_raw.isdigit():
                raise RuntimeError(
                    f"Invalid topic id for group {group_id_raw}: {topic_id_raw}"
                )

            topic_id = int(topic_id_raw)
            topic_key = str(topic_id)

            if topic_key in topic_map:
                raise RuntimeError(
                    f"Duplicate topic id in group {group_id_raw}: {topic_id}"
                )

            topic_map[topic_key] = {
                "name": f"Topic {topic_id}",
                "message_thread_id": topic_id,
            }

        destinations[destination_key] = {
            "name": f"المجموعة {index}",
            "chat_id": chat_id,
            "topics": topic_map,
        }

    return destinations


DESTINATIONS = load_destinations()


# ============================================================
# LOGGING
# ============================================================

def log(*args):
    print(*args, flush=True)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return psycopg.connect(
        DATABASE_URL,
        sslmode="require",
    )


def init_publication_db():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists course_publications (
                    id bigserial primary key,
                    admin_user_id bigint not null,
                    admin_chat_id bigint not null,
                    admin_message_id bigint not null,
                    prompt_message_id bigint,
                    title text,
                    state text not null,
                    destination_key text,
                    topic_key text,
                    created_at timestamptz not null default now(),
                    updated_at timestamptz not null default now()
                )
                """
            )

            cur.execute(
                """
                create index if not exists idx_course_publications_admin_state
                on course_publications (admin_user_id, state, updated_at desc)
                """
            )

            conn.commit()


def create_publication_state(
    admin_user_id,
    admin_chat_id,
    admin_message_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into course_publications
                    (
                        admin_user_id,
                        admin_chat_id,
                        admin_message_id,
                        state
                    )
                values
                    (%s, %s, %s, 'awaiting_title')
                returning id
                """,
                (
                    admin_user_id,
                    admin_chat_id,
                    admin_message_id,
                )
            )

            publication_id = cur.fetchone()[0]
            conn.commit()
            return publication_id


def set_prompt_message(
    publication_id,
    prompt_message_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update course_publications
                set prompt_message_id = %s,
                    updated_at = now()
                where id = %s
                """,
                (
                    prompt_message_id,
                    publication_id,
                )
            )
            conn.commit()


def get_publication(publication_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    id,
                    admin_user_id,
                    admin_chat_id,
                    admin_message_id,
                    prompt_message_id,
                    title,
                    state,
                    destination_key,
                    topic_key
                from course_publications
                where id = %s
                """,
                (publication_id,)
            )

            row = cur.fetchone()

    if not row:
        return None

    keys = [
        "id",
        "admin_user_id",
        "admin_chat_id",
        "admin_message_id",
        "prompt_message_id",
        "title",
        "state",
        "destination_key",
        "topic_key",
    ]

    return dict(zip(keys, row))


def get_latest_awaiting_title(admin_user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    id,
                    admin_user_id,
                    admin_chat_id,
                    admin_message_id,
                    prompt_message_id,
                    title,
                    state,
                    destination_key,
                    topic_key
                from course_publications
                where admin_user_id = %s
                  and state = 'awaiting_title'
                order by updated_at desc
                limit 1
                """,
                (admin_user_id,)
            )

            row = cur.fetchone()

    if not row:
        return None

    keys = [
        "id",
        "admin_user_id",
        "admin_chat_id",
        "admin_message_id",
        "prompt_message_id",
        "title",
        "state",
        "destination_key",
        "topic_key",
    ]

    return dict(zip(keys, row))


def update_publication(
    publication_id,
    **fields,
):
    allowed = {
        "prompt_message_id",
        "title",
        "state",
        "destination_key",
        "topic_key",
    }

    updates = []
    values = []

    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(
                f"Invalid publication field: {key}"
            )

        updates.append(
            f"{key} = %s"
        )
        values.append(value)

    if not updates:
        return

    values.append(publication_id)

    query = (
        "update course_publications "
        "set "
        + ", ".join(updates)
        + ", updated_at = now() "
        + "where id = %s"
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, values)
            conn.commit()


# ============================================================
# TELEGRAM API
# ============================================================

def tg(
    method,
    payload=None,
    files=None,
    timeout=90,
):
    payload = payload or {}
    url = f"{TG_API}/{method}"

    log(
        "========== TELEGRAM DEBUG =========="
    )
    log(
        "METHOD:",
        method,
    )

    try:
        if files:
            response = requests.post(
                url,
                data=payload,
                files=files,
                timeout=timeout,
            )
        else:
            response = requests.post(
                url,
                json=payload,
                timeout=timeout,
            )

    except Exception as exc:
        log(
            "Telegram request exception:",
            repr(exc),
        )
        raise

    log(
        "STATUS:",
        response.status_code,
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
        flags=re.UNICODE,
    )

    filename = filename.strip()

    return (
        filename[:180]
        or "course.bin"
    )


def escape_html(value):
    return html.escape(
        str(value or ""),
        quote=False,
    )


def check_secret(
    header_name,
    expected,
):
    supplied = request.headers.get(
        header_name,
        "",
    )

    return (
        bool(supplied)
        and hmac.compare_digest(
            supplied,
            expected,
        )
    )


def build_review_caption(
    source,
    sender,
    filename,
    caption,
    digest,
    title=None,
    status="⏳ بانتظار العنوان",
):
    title_line = (
        f"🏷 العنوان: {escape_html(title)}\n"
        if title
        else "🏷 العنوان: <i>سيتم طلبه عند النشر</i>\n"
    )

    return (
        "📥 <b>طلب كور جديد</b>\n\n"
        f"📌 المصدر: {escape_html(source)}\n"
        f"👤 المرسل: {escape_html(sender)}\n"
        f"📄 الملف: {escape_html(filename)}\n"
        f"📝 الملاحظة: {escape_html(caption or '—')}\n"
        f"🔐 SHA256: {escape_html(digest[:16])}…\n"
        f"{title_line}\n"
        f"الحالة: {status}"
    )


def build_review_keyboard():
    return {
        "inline_keyboard": [[
            {
                "text": "✅ نشر الدرس",
                "callback_data": "approve",
            },
            {
                "text": "❌ رفض",
                "callback_data": "reject",
            },
        ]]
    }


def build_destination_keyboard(publication_id):
    rows = []
    current = []

    for key, destination in DESTINATIONS.items():
        current.append({
            "text": destination["name"],
            "callback_data": f"pub:g:{publication_id}:{key}",
        })

        if len(current) == 2:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    return {
        "inline_keyboard": rows
    }


def build_topic_keyboard(
    publication_id,
    destination_key,
):
    destination = DESTINATIONS[destination_key]
    rows = []
    current = []

    for topic_key, topic in destination["topics"].items():
        current.append({
            "text": topic["name"],
            "callback_data": (
                f"pub:t:{publication_id}:{destination_key}:{topic_key}"
            ),
        })

        if len(current) == 2:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    rows.append([
        {
            "text": "↩️ رجوع للمجموعات",
            "callback_data": f"pub:back:{publication_id}",
        }
    ])

    return {
        "inline_keyboard": rows
    }


def admin_message_caption_update(
    message,
    title=None,
    status=None,
):
    caption = message.get("caption", "")

    if title:
        caption += (
            f"\n\n🏷 <b>العنوان:</b> "
            f"{escape_html(title)}"
        )

    if status:
        caption += (
            f"\n<b>الحالة:</b> {status}"
        )

    return caption[:1024]


def is_admin_message(
    callback,
):
    return callback.get("from", {}).get("id") in ADMIN_IDS


def answer_callback(
    callback_id,
    text,
):
    try:
        tg(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text,
            }
        )
    except Exception as exc:
        log(
            "answerCallbackQuery error:",
            repr(exc),
        )


def send_title_prompt(
    admin_chat_id,
):
    return tg(
        "sendMessage",
        {
            "chat_id": admin_chat_id,
            "text": (
                "📝 <b>أرسل عنوان الدرس</b>\n\n"
                "العنوان إجباري.\n"
                "مثال: Immunologie — Chapitre 3"
            ),
            "parse_mode": "HTML",
            "reply_markup": {
                "force_reply": True,
                "input_field_placeholder": "عنوان الدرس...",
                "selective": True,
            },
        }
    )


def edit_admin_status(
    chat_id,
    message_id,
    caption,
    reply_markup=None,
):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return tg(
        "editMessageCaption",
        payload,
    )


# ============================================================
# SEND TO ADMINS
# ============================================================

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

    review_caption = build_review_caption(
        source=source,
        sender=sender,
        filename=clean_name,
        caption=caption,
        digest=digest,
    )

    first_message_id = None

    for admin_id in ADMIN_IDS:
        result = tg(
            "sendDocument",
            payload={
                "chat_id": admin_id,
                "caption": review_caption[:1024],
                "parse_mode": "HTML",
                "reply_markup": json.dumps(
                    build_review_keyboard(),
                    ensure_ascii=False,
                ),
            },
            files={
                "document": (
                    clean_name,
                    data,
                    "application/octet-stream",
                )
            },
        )

        if first_message_id is None:
            first_message_id = result["message_id"]

    return first_message_id


# ============================================================
# PUBLICATION WIZARD
# ============================================================

def start_publication_wizard(
    callback,
    message,
):
    admin_user_id = callback["from"]["id"]
    admin_chat_id = message["chat"]["id"]
    admin_message_id = message["message_id"]

    publication_id = create_publication_state(
        admin_user_id=admin_user_id,
        admin_chat_id=admin_chat_id,
        admin_message_id=admin_message_id,
    )

    prompt = send_title_prompt(
        admin_chat_id
    )

    set_prompt_message(
        publication_id,
        prompt["message_id"],
    )

    edit_admin_status(
        admin_chat_id,
        admin_message_id,
        admin_message_caption_update(
            message,
            status="📝 بانتظار عنوان الدرس",
        ),
        reply_markup={
            "inline_keyboard": [[
                {
                    "text": "↩️ إلغاء",
                    "callback_data": (
                        f"pub:cancel:{publication_id}"
                    ),
                }
            ]]
        },
    )

    answer_callback(
        callback["id"],
        "أرسل عنوان الدرس في رسالة الرد."
    )


def handle_title_message(
    message,
):
    admin_user_id = message.get(
        "from",
        {}
    ).get("id")

    if admin_user_id not in ADMIN_IDS:
        return False

    reply_to = message.get(
        "reply_to_message"
    )

    publication = None

    if reply_to:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                        id,
                        admin_user_id,
                        admin_chat_id,
                        admin_message_id,
                        prompt_message_id,
                        title,
                        state,
                        destination_key,
                        topic_key
                    from course_publications
                    where admin_user_id = %s
                      and prompt_message_id = %s
                      and state = 'awaiting_title'
                    order by updated_at desc
                    limit 1
                    """,
                    (
                        admin_user_id,
                        reply_to.get("message_id"),
                    )
                )
                row = cur.fetchone()

        if row:
            keys = [
                "id",
                "admin_user_id",
                "admin_chat_id",
                "admin_message_id",
                "prompt_message_id",
                "title",
                "state",
                "destination_key",
                "topic_key",
            ]
            publication = dict(zip(keys, row))

    if not publication:
        return False

    title = str(
        message.get("text", "")
    ).strip()

    if not title:
        tg(
            "sendMessage",
            {
                "chat_id": message["chat"]["id"],
                "text": (
                    "❗ عنوان الدرس إجباري.\n"
                    "أرسل عنوانًا غير فارغ."
                ),
                "reply_to_message_id": message["message_id"],
            }
        )
        return True

    if len(title) > MAX_TITLE_LENGTH:
        tg(
            "sendMessage",
            {
                "chat_id": message["chat"]["id"],
                "text": (
                    f"❗ العنوان طويل جدًا. الحد الأقصى "
                    f"{MAX_TITLE_LENGTH} حرفًا."
                ),
                "reply_to_message_id": message["message_id"],
            }
        )
        return True

    update_publication(
        publication["id"],
        title=title,
        state="choosing_destination",
    )

    # We already know the original message id, so only update its caption.
    edit_admin_status(
        publication["admin_chat_id"],
        publication["admin_message_id"],
        (
            f"🏷 <b>العنوان:</b> {escape_html(title)}\n"
            "<b>الحالة:</b> 📤 اختر المجموعة"
        ),
        reply_markup=build_destination_keyboard(publication["id"]),
    )

    tg(
        "sendMessage",
        {
            "chat_id": publication["admin_chat_id"],
            "text": (
                f"✅ تم حفظ العنوان: <b>{escape_html(title)}</b>\n\n"
                "📚 اختر المجموعة التي سينشر فيها الدرس:"
            ),
            "parse_mode": "HTML",
            "reply_markup": build_destination_keyboard(publication["id"]),
        }
    )

    return True


def publish_publication(
    publication_id,
    destination_key,
    topic_key=None,
):
    publication = get_publication(
        publication_id
    )

    if not publication:
        raise ValueError(
            "Publication request not found."
        )

    if publication["state"] not in {
        "choosing_destination",
        "choosing_topic",
    }:
        raise ValueError(
            "Publication request is not ready."
        )

    if publication["admin_user_id"] not in ADMIN_IDS:
        raise PermissionError(
            "Unauthorized publication request."
        )

    destination = DESTINATIONS.get(
        destination_key
    )

    if not destination:
        raise ValueError(
            "Unknown destination."
        )

    thread_id = None

    if topic_key:
        topic = destination["topics"].get(
            topic_key
        )

        if not topic:
            raise ValueError(
                "Unknown topic."
            )

        thread_id = topic["message_thread_id"]

    elif destination["topics"]:
        raise ValueError(
            "A topic is required for this destination."
        )

    title = str(publication.get("title") or "").strip()
    if not title:
        raise ValueError("عنوان الدرس مفقود.")

    payload = {
        "chat_id": destination["chat_id"],
        "from_chat_id": publication["admin_chat_id"],
        "message_id": publication["admin_message_id"],
        # Show only the lesson title to students; do not publish the admin
        # review metadata/source information.
        "caption": escape_html(title),
        "parse_mode": "HTML",
    }

    if thread_id is not None:
        payload["message_thread_id"] = thread_id

    result = tg(
        "copyMessage",
        payload,
    )

    update_publication(
        publication_id,
        state="published",
        destination_key=destination_key,
        topic_key=topic_key,
    )

    return result, destination


def cancel_publication(
    publication_id,
):
    publication = get_publication(
        publication_id
    )

    if not publication:
        return None

    update_publication(
        publication_id,
        state="cancelled",
    )

    return publication


# ============================================================
# APPROVE / REJECT
# ============================================================

def reject_admin_message(
    message,
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

    edit_admin_status(
        chat_id,
        message["message_id"],
        rejected_caption,
        reply_markup={
            "inline_keyboard": []
        },
    )


# ============================================================
# BASIC
# ============================================================

@app.get("/")
def index():
    return (
        "Course Center Bot is alive.",
        200,
    )


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "admin_count": len(ADMIN_IDS),
        "destinations": len(DESTINATIONS),
        "topics": sum(
            len(item["topics"])
            for item in DESTINATIONS.values()
        ),
        "whatsapp_gateway_configured": bool(
            WHATSAPP_GATEWAY_SECRET
        ),
        "email_cron_configured": bool(
            EMAIL_CRON_SECRET
            and GITHUB_ACTIONS_TOKEN
        ),
    })


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/webhook/telegram")
def telegram_webhook():
    if not check_secret(
        "X-Telegram-Bot-Api-Secret-Token",
        TELEGRAM_WEBHOOK_SECRET,
    ):
        log(
            "Telegram webhook: invalid secret."
        )
        return (
            "forbidden",
            403,
        )

    update = request.get_json(
        silent=True
    ) or {}

    message_update = update.get("message")
    if message_update:
        try:
            if handle_title_message(
                message_update
            ):
                return jsonify({
                    "ok": True
                })
        except Exception as exc:
            log(
                "Telegram title handling error:",
                repr(exc),
            )
            return jsonify({
                "ok": False,
                "error": str(exc),
            }), 500

        return jsonify({
            "ok": True
        })

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
    ).get("id")

    if admin_id not in ADMIN_IDS:
        answer_callback(
            callback["id"],
            "غير مسموح.",
        )
        return jsonify({
            "ok": True
        })

    action = callback.get(
        "data",
        "",
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
            if not message.get("document"):
                raise ValueError(
                    "الرسالة لا تحتوي على document."
                )

            start_publication_wizard(
                callback,
                message,
            )

            return jsonify({
                "ok": True
            })

        if action == "reject":
            reject_admin_message(message)
            answer_callback(
                callback["id"],
                "تم الرفض ❌",
            )
            return jsonify({
                "ok": True
            })

        if action.startswith("pub:cancel:"):
            publication_id = int(
                action.split(":", 2)[2]
            )

            publication = cancel_publication(
                publication_id
            )

            if publication:
                edit_admin_status(
                    publication["admin_chat_id"],
                    publication["admin_message_id"],
                    (
                        "❌ <b>تم إلغاء عملية النشر.</b>"
                    ),
                    reply_markup={
                        "inline_keyboard": []
                    },
                )

                answer_callback(
                    callback["id"],
                    "تم الإلغاء.",
                )

            return jsonify({
                "ok": True
            })

        if action.startswith("pub:back:"):
            publication_id = int(action.split(":", 2)[2])
            publication = get_publication(publication_id)

            if not publication or publication["admin_user_id"] != admin_id:
                raise PermissionError(
                    "عملية النشر غير صالحة."
                )

            update_publication(
                publication_id,
                state="choosing_destination",
                destination_key=None,
                topic_key=None,
            )

            tg(
                "sendMessage",
                {
                    "chat_id": publication["admin_chat_id"],
                    "text": "📚 اختر المجموعة التي تريد وضع الدرس فيها:",
                    "reply_markup": build_destination_keyboard(publication_id),
                }
            )

            answer_callback(
                callback["id"],
                "اختر المجموعة.",
            )
            return jsonify({
                "ok": True
            })

        if action.startswith("pub:g:"):
            parts = action.split(":", 3)
            if len(parts) != 4:
                raise ValueError(
                    "Invalid destination callback."
                )

            publication_id = int(parts[2])
            destination_key = parts[3]
            destination = DESTINATIONS.get(
                destination_key
            )

            if not destination:
                raise ValueError(
                    "المجموعة غير موجودة."
                )

            publication = get_publication(publication_id)
            if not publication or publication["admin_user_id"] != admin_id:
                raise PermissionError(
                    "عملية النشر غير صالحة."
                )

            if publication["state"] != "choosing_destination":
                raise ValueError(
                    "عملية النشر ليست في مرحلة اختيار المجموعة."
                )

            if destination["topics"]:
                update_publication(
                    publication_id,
                    state="choosing_topic",
                    destination_key=destination_key,
                )

                tg(
                    "sendMessage",
                    {
                        "chat_id": message["chat"]["id"],
                        "text": (
                            f"📚 <b>{escape_html(destination['name'])}</b>\n\n"
                            "🧵 اختر التوبيك الذي تريد وضع الدرس فيه:"
                        ),
                        "parse_mode": "HTML",
                        "reply_markup": build_topic_keyboard(
                            publication_id,
                            destination_key
                        ),
                    }
                )

                answer_callback(
                    callback["id"],
                    "اختر التوبيك.",
                )
                return jsonify({
                    "ok": True
                })

            result, destination = publish_publication(
                publication_id,
                destination_key,
                None,
            )

            publication = get_publication(
                publication_id
            )

            if publication:
                edit_admin_status(
                    publication["admin_chat_id"],
                    publication["admin_message_id"],
                    (
                        f"🏷 <b>العنوان:</b> "
                        f"{escape_html(publication['title'])}\n"
                        f"✅ <b>تم النشر في:</b> "
                        f"{escape_html(destination['name'])}"
                    ),
                    reply_markup={
                        "inline_keyboard": []
                    },
                )

            answer_callback(
                callback["id"],
                "تم النشر ✅",
            )

            return jsonify({
                "ok": True,
                "message_id": result.get("message_id"),
            })

        if action.startswith("pub:t:"):
            parts = action.split(":", 4)
            if len(parts) != 5:
                raise ValueError(
                    "Invalid topic callback."
                )

            publication_id = int(parts[2])
            destination_key = parts[3]
            topic_key = parts[4]

            publication = get_publication(publication_id)
            if not publication or publication["admin_user_id"] != admin_id:
                raise PermissionError(
                    "عملية النشر غير صالحة."
                )

            if publication["state"] != "choosing_topic":
                raise ValueError(
                    "عملية النشر ليست في مرحلة اختيار التوبيك."
                )

            result, destination = publish_publication(
                publication_id,
                destination_key,
                topic_key,
            )

            publication = get_publication(
                publication_id
            )

            topic_name = destination[
                "topics"
            ][topic_key]["name"]

            if publication:
                edit_admin_status(
                    publication["admin_chat_id"],
                    publication["admin_message_id"],
                    (
                        f"🏷 <b>العنوان:</b> "
                        f"{escape_html(publication['title'])}\n"
                        f"✅ <b>تم النشر في:</b> "
                        f"{escape_html(destination['name'])}\n"
                        f"🧵 <b>التوبيك:</b> "
                        f"{escape_html(topic_name)}"
                    ),
                    reply_markup={
                        "inline_keyboard": []
                    },
                )

            answer_callback(
                callback["id"],
                "تم النشر في التوبيك ✅",
            )

            return jsonify({
                "ok": True,
                "message_id": result.get("message_id"),
            })

    except Exception as exc:
        log(
            "Telegram callback error:",
            repr(exc),
        )

        answer_callback(
            callback["id"],
            "حدث خطأ، راجع اللوج.",
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
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
        EMAIL_INGEST_SECRET,
    ):
        return (
            "forbidden",
            403,
        )

    sender = request.form.get(
        "sender",
        "unknown",
    )
    subject = request.form.get(
        "subject",
        "",
    )
    filename = request.form.get(
        "filename",
        "course.bin",
    )
    uploaded = request.files.get(
        "file"
    )

    if uploaded is None:
        return jsonify({
            "ok": False,
            "error": "file missing",
        }), 400

    data = uploaded.read()

    try:
        message_id = send_submission_to_admins(
            source="email",
            sender=sender,
            filename=filename,
            data=data,
            caption=subject,
        )

        return jsonify({
            "ok": True,
            "admin_message_id": message_id,
        })

    except Exception as exc:
        log(
            "Email ingest error:",
            repr(exc),
        )
        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# ============================================================
# WHATSAPP GATEWAY INGEST
# ============================================================

@app.post("/ingest/whatsapp")
def ingest_whatsapp_gateway():
    if not check_secret(
        "X-WhatsApp-Gateway-Secret",
        WHATSAPP_GATEWAY_SECRET,
    ):
        return (
            "forbidden",
            403,
        )

    sender = request.form.get(
        "sender",
        "unknown",
    )
    caption = request.form.get(
        "caption",
        "",
    )
    filename = request.form.get(
        "filename",
        "whatsapp_file.bin",
    )
    uploaded = request.files.get(
        "file"
    )

    if uploaded is None:
        return jsonify({
            "ok": False,
            "error": "file missing",
        }), 400

    data = uploaded.read()

    try:
        message_id = send_submission_to_admins(
            source="whatsapp",
            sender=sender,
            filename=filename,
            data=data,
            caption=caption,
        )

        return jsonify({
            "ok": True,
            "admin_message_id": message_id,
        })

    except Exception as exc:
        log(
            "WhatsApp gateway ingest error:",
            repr(exc),
        )
        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# ============================================================
# CRON -> GITHUB WORKFLOW
# ============================================================

@app.post("/cron/email")
def trigger_email_workflow():
    if not EMAIL_CRON_SECRET or not GITHUB_ACTIONS_TOKEN:
        return jsonify({
            "ok": False,
            "error": "Email cron is not configured.",
        }), 503

    if not check_secret(
        "X-Cron-Secret",
        EMAIL_CRON_SECRET,
    ):
        return "forbidden", 403

    url = (
        "https://api.github.com/repos/"
        "MahdiBoumaaraf/HealyaBot"
        "/actions/workflows/email.yml/dispatches"
    )

    response = requests.post(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_ACTIONS_TOKEN}",
            "X-GitHub-Api-Version": "2026-03-10",
            "Content-Type": "application/json",
        },
        json={
            "ref": "main"
        },
        timeout=30,
    )

    log(
        "GitHub workflow dispatch:",
        response.status_code,
    )

    if response.status_code not in (200, 204):
        return jsonify({
            "ok": False,
            "github_status": response.status_code,
            "error": response.text,
        }), 502

    return jsonify({
        "ok": True,
        "message": "Email workflow dispatched.",
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    init_publication_db()

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
        "Course destinations:",
        len(DESTINATIONS)
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
