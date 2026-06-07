#!/usr/bin/env bash
# ================================================================
#  start_bot.sh — ตัวเปิดบอทสำหรับ VPS (Git Bash / Linux)
#  ใช้แทน start_loop.bat เมื่อรันผ่าน Git Bash บน Windows VPS
#  หรือรันผ่าน Linux ตรง
#
#  วิธีใช้:
#    chmod +x start_bot.sh
#    nohup ./start_bot.sh >> part2.log 2>&1 &
#
#  วิธีหยุด:
#    1. Telegram: พิมพ์ /stop   (หยุดและไม่ restart)
#    2. Telegram: พิมพ์ /restart (restart ใน ~15 วิ)
#    3. ลบไฟล์ flag ด้วยตรง: rm part2_should_run.flag
#    4. SSH: kill $(pgrep -f interactive.py)
# ================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAG="$SCRIPT_DIR/part2_should_run.flag"
PYTHON="${PYTHON:-python}"

echo "[$(date '+%F %T')] === Part 2 Bot: start_bot.sh เริ่มทำงาน ===" | tee -a "$SCRIPT_DIR/part2.log"

# สร้าง flag ถ้ายังไม่มี (บอท run ต่อ)
touch "$FLAG"

while [ -f "$FLAG" ]; do
    echo "[$(date '+%F %T')] Starting interactive.py ..." | tee -a "$SCRIPT_DIR/part2.log"

    # รัน bot — exit code ใดก็ตาม loop จัดการ
    "$PYTHON" "$SCRIPT_DIR/interactive.py" >> "$SCRIPT_DIR/part2.log" 2>&1 || true

    # ตรวจ flag หลัง exit — ถ้าหาย = /stop กด → ไม่ restart
    if [ ! -f "$FLAG" ]; then
        echo "[$(date '+%F %T')] 🛑 Bot หยุดแล้ว (flag ถูกลบ = /stop จาก Telegram)" | tee -a "$SCRIPT_DIR/part2.log"
        break
    fi

    echo "[$(date '+%F %T')] ⚠️ Bot exit แล้ว — restart ใน 15 วิ..." | tee -a "$SCRIPT_DIR/part2.log"
    sleep 15
done

echo "[$(date '+%F %T')] === start_bot.sh สิ้นสุด ===" | tee -a "$SCRIPT_DIR/part2.log"
