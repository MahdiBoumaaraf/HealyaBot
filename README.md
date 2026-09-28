# Course Center Bot — Free Hosting Edition

## لماذا هذه النسخة؟

Render Free ينام بعد فترة من عدم وجود طلبات، وملفاته المحلية مؤقتة. لذلك لا نستعمل:
- polling مستمر
- SQLite محلية
- حفظ الملفات على disk المحلي

بدل ذلك:
- Telegram = Webhook
- WhatsApp = Webhook
- Email = GitHub Actions كل 15 دقيقة
- الملف نفسه يُرسل مباشرة إلى حساب Telegram الخاص بالأدمن
- Telegram يحتفظ بنسخة الملف
- عند الضغط على "قبول" يستخدم البوت `copyMessage` وينشر نفس الرسالة في غروب الطلبة

## الاستضافة

### Render Free

أنشئ Web Service من GitHub:
- Runtime: Python
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Plan: Free

بعد النشر ستحصل على:
`https://YOUR-SERVICE.onrender.com`

### متغيرات Render

أضف:

- TELEGRAM_BOT_TOKEN
- CENTER_CHAT_ID
- ADMIN_IDS
- TELEGRAM_WEBHOOK_SECRET
- EMAIL_INGEST_SECRET
- WHATSAPP_VERIFY_TOKEN
- WHATSAPP_ACCESS_TOKEN
- WHATSAPP_GRAPH_VERSION

### Telegram Webhook

بعد معرفة رابط Render شغّل:

```bash
curl -X POST "https://api.telegram.org/botYOUR_TOKEN/setWebhook" \
  -d "url=https://YOUR-SERVICE.onrender.com/webhook/telegram" \
  -d "secret_token=YOUR_TELEGRAM_WEBHOOK_SECRET" \
  -d 'allowed_updates=["callback_query"]'
```

هذا يجعل Telegram يرسل ضغطات الموافقة/الرفض إلى Render.

## غروب السونتر

`CENTER_CHAT_ID` هو معرف غروب الطلبة الموجود أصلاً في Telegram.

البوت يجب أن يكون عضوًا فيه مع صلاحية إرسال المستندات.

## Email عبر GitHub Actions

ضع هذه الملفات في GitHub:
- `email_poller.py`
- `.github/workflows/email.yml`

لا تضع كلمة مرور البريد داخل الكود.

أضف GitHub repository secrets:

- IMAP_HOST
- IMAP_PORT
- EMAIL_USERNAME
- EMAIL_PASSWORD
- EMAIL_FOLDER
- INGEST_URL = `https://YOUR-SERVICE.onrender.com/ingest/email`
- EMAIL_INGEST_SECRET

الـworkflow يعمل كل 15 دقيقة.

يمكن تشغيله يدويًا من GitHub Actions عبر `workflow_dispatch`.

## WhatsApp

في Meta/WhatsApp Cloud API استخدم:

Webhook URL:

`https://YOUR-SERVICE.onrender.com/webhook/whatsapp`

Verify Token:

نفس قيمة `WHATSAPP_VERIFY_TOKEN`.

Access Token:

قيمة سرية في Render فقط.

## ملاحظة حول المجانية

Render Free مناسب للمشروع الطلابي التجريبي، لكنه ليس استضافة إنتاج مضمونة:
- الخدمة قد تنام بعد 15 دقيقة من عدم وجود طلبات.
- عند الاستيقاظ قد تتأخر حوالي دقيقة.
- الملفات المحلية مؤقتة.

لهذا التصميم لا يعتمد على الملفات المحلية، والملف يُحفظ عمليًا في Telegram من خلال رسالة المراجعة.

GitHub Actions مناسب لفحص البريد على فترات. أقصر جدولة مدعومة حاليًا هي كل 5 دقائق؛ هنا استخدمنا 15 دقيقة لتقليل التشغيل غير الضروري.

## الأمان

لا ترفع `.env`.
لا تضع:
- Telegram bot token
- WhatsApp access token
- Email password
- webhook secrets

داخل GitHub أو الكود العام.

إذا كان repository عامًا، استخدم GitHub Secrets للقيم السرية.
