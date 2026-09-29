#!/bin/sh
set -eu

node whatsapp_gateway.js &
NODE_PID=$!

cleanup() {
  kill "$NODE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec /opt/venv/bin/python bot.py
