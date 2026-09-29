import fs from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import pg from 'pg'
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  normalizeMessageContent,
  useMultiFileAuthState,
} from 'baileys'

const { Pool } = pg

const DATABASE_URL = process.env.DATABASE_URL?.trim()
const GATEWAY_SECRET = process.env.WHATSAPP_GATEWAY_SECRET?.trim()
const PYTHON_PORT = Number(process.env.PORT || 8080)
const PAIRING_PHONE = (process.env.WHATSAPP_PAIRING_PHONE || '').replace(/\D/g, '')
const INTERNAL_URL = `http://127.0.0.1:${PYTHON_PORT}/ingest/whatsapp`
const AUTH_DIR = path.resolve('./.wa_auth')
const SESSION_NAME = 'healyabot'
const SNAPSHOT_INTERVAL_MS = 5000

if (!DATABASE_URL) {
  console.error('DATABASE_URL is missing. WhatsApp gateway will not start.')
  process.exit(1)
}

if (!GATEWAY_SECRET) {
  console.error('WHATSAPP_GATEWAY_SECRET is missing. WhatsApp gateway will not start.')
  process.exit(1)
}

const pool = new Pool({
  connectionString: DATABASE_URL,
  ssl: { rejectUnauthorized: false },
  max: 3,
})

const logger = pino({ level: 'warn' })
let pairingRequested = false
let reconnectTimer = null
let stopRequested = false
let socket = null
const knownFiles = new Map()

async function ensureAuthTable() {
  await pool.query(`
    create table if not exists baileys_auth_files (
      session text not null,
      name text not null,
      data bytea not null,
      updated_at timestamptz not null default now(),
      primary key (session, name)
    )
  `)
}

function safeFileName(name) {
  const clean = path.basename(name)
  if (!clean || clean === '.' || clean === '..') {
    throw new Error(`Unsafe auth filename: ${name}`)
  }
  return clean
}

async function restoreAuthFolder() {
  await fs.mkdir(AUTH_DIR, { recursive: true })
  const { rows } = await pool.query(
    'select name, data from baileys_auth_files where session = $1',
    [SESSION_NAME]
  )

  for (const row of rows) {
    const fileName = safeFileName(row.name)
    await fs.writeFile(path.join(AUTH_DIR, fileName), row.data)
  }

  console.log(`Restored ${rows.length} WhatsApp auth files from PostgreSQL.`)
}

async function listAuthFiles() {
  await fs.mkdir(AUTH_DIR, { recursive: true })
  const entries = await fs.readdir(AUTH_DIR, { withFileTypes: true })
  return entries
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name)
    .sort()
}

async function syncAuthFolder() {
  const names = await listAuthFiles()
  const seen = new Set(names)

  for (const name of names) {
    const fullPath = path.join(AUTH_DIR, name)
    const stat = await fs.stat(fullPath)
    const signature = `${stat.size}:${stat.mtimeMs}`

    if (knownFiles.get(name) === signature) continue

    const data = await fs.readFile(fullPath)
    await pool.query(
      `
        insert into baileys_auth_files (session, name, data, updated_at)
        values ($1, $2, $3, now())
        on conflict (session, name)
        do update set data = excluded.data, updated_at = now()
      `,
      [SESSION_NAME, name, data]
    )
    knownFiles.set(name, signature)
  }

  for (const name of Array.from(knownFiles.keys())) {
    if (seen.has(name)) continue

    await pool.query(
      'delete from baileys_auth_files where session = $1 and name = $2',
      [SESSION_NAME, name]
    )
    knownFiles.delete(name)
  }
}

async function restoreKnownFileSignatures() {
  for (const name of await listAuthFiles()) {
    const stat = await fs.stat(path.join(AUTH_DIR, name))
    knownFiles.set(name, `${stat.size}:${stat.mtimeMs}`)
  }
}

function extractMediaMessage(message) {
  const content = normalizeMessageContent(message.message)
  if (!content) return null

  const type = getContentType(content)

  if (type === 'documentMessage') {
    return { type, media: content.documentMessage }
  }

  if (type === 'imageMessage') {
    return { type, media: content.imageMessage }
  }

  return null
}

async function sendToHealyaBot(message, mediaInfo, data) {
  const jid = message.key.remoteJid || 'unknown'
  const pushName = message.pushName || 'unknown'
  const sender = `${pushName} (${jid})`
  const media = mediaInfo.media || {}

  let filename = media.fileName

  if (!filename && mediaInfo.type === 'imageMessage') {
    const mime = String(media.mimetype || 'image/jpeg')
    const ext = mime.split('/')[1] || 'jpg'
    filename = `whatsapp_image.${ext}`
  }

  filename = filename || 'whatsapp_file.bin'

  const caption = media.caption || ''
  const mime = media.mimetype || 'application/octet-stream'

  const form = new FormData()
  form.append('sender', sender)
  form.append('caption', caption)
  form.append('filename', filename)
  form.append(
    'file',
    new Blob([data], { type: mime }),
    filename
  )

  const response = await fetch(INTERNAL_URL, {
    method: 'POST',
    headers: {
      'X-WhatsApp-Gateway-Secret': GATEWAY_SECRET,
    },
    body: form,
  })

  const text = await response.text()
  console.log(
    `HealyaBot ingest response: ${response.status} ${text}`
  )

  if (!response.ok) {
    throw new Error(
      `HealyaBot ingest failed: ${response.status}`
    )
  }
}

async function startSocket() {
  if (stopRequested) return

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)

  socket = makeWASocket({
    auth: state,
    logger,
    markOnlineOnConnect: false,
  })

  // When a pairing phone is provided, use WhatsApp's 8-character
  // pairing code instead of relying on an ASCII QR rendered in logs.
  // This is much easier to use on hosted platforms such as Render.
  socket.ev.on('creds.update', async () => {
    try {
      await saveCreds()
    } catch (error) {
      console.error('WhatsApp saveCreds error:', error)
    }
  })

  socket.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update

    // Prefer pairing code when WHATSAPP_PAIRING_PHONE is configured.
    // The hosted Render log can wrap the ASCII QR, which makes scanning
    // unreliable. Baileys supports an 8-character pairing code instead.
    if (
      connection === 'connecting' &&
      PAIRING_PHONE &&
      !state.creds.registered &&
      !pairingRequested
    ) {
      pairingRequested = true

      try {
        const code = await socket.requestPairingCode(
          PAIRING_PHONE
        )

        console.log(
          '========== WHATSAPP PAIRING CODE =========='
        )
        console.log(code)
        console.log(
          '==========================================='
        )
        console.log(
          'WhatsApp -> Linked devices -> Link a device -> Link with phone number'
        )
      } catch (error) {
        pairingRequested = false
        console.error(
          'Pairing code error:',
          error
        )
      }
    }

    // Only show a QR if no pairing phone was configured.
    if (qr && !PAIRING_PHONE) {
      console.log('========== WHATSAPP QR ==========')
      qrcode.generate(qr, { small: true })
      console.log(
        'Scan it from WhatsApp -> Linked devices -> Link a device.'
      )
      console.log('=================================')
    }

    if (connection === 'open') {
      pairingRequested = false
      console.log('✅ WhatsApp connected successfully.')
    }

    if (connection === 'close') {
      const status = lastDisconnect?.error?.output?.statusCode
      const loggedOut = status === DisconnectReason.loggedOut

      console.log(
        `WhatsApp connection closed. status=${status ?? 'unknown'} loggedOut=${loggedOut}`
      )

      if (!stopRequested && !loggedOut) {
        if (reconnectTimer) clearTimeout(reconnectTimer)
        reconnectTimer = setTimeout(() => {
          startSocket().catch((error) => {
            console.error('WhatsApp reconnect error:', error)
          })
        }, 5000)
      } else if (loggedOut) {
        console.error(
          'WhatsApp logged out. A fresh QR/pairing is required.'
        )
      }
    }
  })

  socket.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return

    for (const message of messages) {
      try {
        if (!message.message) continue
        if (message.key.fromMe) continue

        const mediaInfo = extractMediaMessage(message)
        if (!mediaInfo) continue

        console.log(
          `WhatsApp media received from ${message.key.remoteJid}`
        )

        const buffer = await downloadMediaMessage(
          message,
          'buffer',
          {},
          {
            logger,
            reuploadRequest: socket.updateMediaMessage,
          }
        )

        await sendToHealyaBot(
          message,
          mediaInfo,
          buffer
        )
      } catch (error) {
        console.error(
          'WhatsApp message handling error:',
          error
        )
      }
    }
  })
}

async function shutdown(signal) {
  stopRequested = true
  if (reconnectTimer) clearTimeout(reconnectTimer)

  try {
    await syncAuthFolder()
  } catch (error) {
    console.error('Final auth sync failed:', error)
  }

  try {
    await pool.end()
  } catch {}

  console.log(`WhatsApp gateway stopped (${signal}).`)
  process.exit(0)
}

async function main() {
  await ensureAuthTable()
  await restoreAuthFolder()
  await restoreKnownFileSignatures()

  setInterval(() => {
    syncAuthFolder().catch((error) => {
      console.error('Auth sync error:', error)
    })
  }, SNAPSHOT_INTERVAL_MS)

  await startSocket()
  console.log('WhatsApp gateway started.')
}

process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))

main().catch((error) => {
  console.error('WhatsApp gateway fatal error:', error)
  process.exit(1)
})
