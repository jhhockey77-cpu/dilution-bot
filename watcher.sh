#!/bin/bash
# dilution-bot watcher — polls GitHub every 60s, restarts bot on change
REPO="https://raw.githubusercontent.com/jhhockey77-cpu/dilution-bot/main/bot_final.py"
BOT="$HOME/dilution_bot.py"
LOG="/tmp/dilution_bot.log"
HASH_FILE="/tmp/dilution_bot_hash"
TOKEN_FILE="$HOME/.dilution_token"

# Load token from local file
TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null)
if [ -z "$TOKEN" ]; then
    echo "[$(date)] ERROR: No token found at $TOKEN_FILE" >> "$LOG"
    exit 1
fi

while true; do
    TMP=$(mktemp)
    curl -fsSL "$REPO" -o "$TMP" 2>/dev/null
    if [ $? -ne 0 ] || [ ! -s "$TMP" ]; then rm -f "$TMP"; sleep 60; continue; fi

    NEW_HASH=$(md5 -q "$TMP")
    OLD_HASH=$(cat "$HASH_FILE" 2>/dev/null)

    if [ "$NEW_HASH" != "$OLD_HASH" ]; then
        echo "[$(date)] New version detected — deploying..." >> "$LOG"
        python3 -c "import ast; ast.parse(open('$TMP').read())" 2>/dev/null
        if [ $? -ne 0 ]; then
            echo "[$(date)] Syntax error — skipping deploy" >> "$LOG"
            rm -f "$TMP"; sleep 60; continue
        fi
        cp "$TMP" "$BOT"
        echo "$NEW_HASH" > "$HASH_FILE"
        pkill -f dilution_bot.py 2>/dev/null; sleep 1
        DISCORD_TOKEN="$TOKEN" PYTHONUNBUFFERED=1 nohup python3 -u "$BOT" >> "$LOG" 2>&1 &
        echo "[$(date)] Bot restarted (PID $!)" >> "$LOG"
    fi
    rm -f "$TMP"; sleep 60
done
