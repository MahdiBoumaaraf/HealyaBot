import os
import imaplib
from email import policy
from email.parser import BytesParser
from pathlib import Path

import requests

IMAP_HOST = os.environ["IMAP_HOST"]
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
EMAIL_USERNAME = os.environ["EMAIL_USERNAME"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_FOLDER = os.getenv("EMAIL_FOLDER", "INBOX")

INGEST_URL = os.environ["INGEST_URL"]
INGEST_SECRET = os.environ["EMAIL_INGEST_SECRET"]

# Use a small allow-list of course/document formats.
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".txt", ".zip"
}

mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
mail.login(EMAIL_USERNAME, EMAIL_PASSWORD)
mail.select(EMAIL_FOLDER)

status, data = mail.search(None, "UNSEEN")
if status != "OK":
    raise SystemExit("IMAP search failed")

for msg_id in data[0].split():
    status, raw = mail.fetch(msg_id, "(RFC822)")
    if status != "OK":
        continue

    message = BytesParser(
        policy=policy.default
    ).parsebytes(raw[0][1])

    sender = str(message.get("From", "unknown"))
    subject = str(message.get("Subject", ""))

    processed = False

    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue

        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            continue

        data_bytes = part.get_payload(decode=True)
        if not data_bytes:
            continue

        response = requests.post(
            INGEST_URL,
            headers={
                "X-Email-Ingest-Secret": INGEST_SECRET,
            },
            data={
                "sender": sender,
                "subject": subject,
                "filename": filename,
            },
            files={
                "file": (
                    filename,
                    data_bytes,
                    part.get_content_type()
                )
            },
            timeout=120,
        )
        response.raise_for_status()
        processed = True

    if processed:
        mail.store(
            msg_id,
            "+FLAGS",
            "\\Seen"
        )

mail.logout()
