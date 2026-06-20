#!/bin/bash
# ============================================================
#  Supervisor AIoT untuk Arduino UNO Q
#  Menjaga aplikasi App Lab selalu hidup. Saat watchdog di main.py
#  keluar (os._exit) karena beku, status app jadi 'stopped/failed',
#  lalu supervisor ini menyalakannya lagi otomatis.
#
#  'arduino-app-cli app start' AMAN dipanggil berkala: jika app sudah
#  'running' ia tidak melakukan apa-apa (lihat source start.go).
#  Perintah ini menjalankan MCU (flash sketch bila perlu) + container
#  Python sekaligus, jadi Bridge MCU<->MPU tetap tersambung.
#
#  Pakai:
#     1) Cari path folder app (yang berisi app.yaml):
#          find ~ -name app.yaml 2>/dev/null
#     2) chmod +x supervisor.sh
#     3) ./supervisor.sh /path/ke/folder/app
#     atau jalankan via systemd (lihat aiot-supervisor.service).
# ============================================================
APP="${1:-$APP_PATH}"
CLI="$(command -v arduino-app-cli || echo /usr/bin/arduino-app-cli)"

if [ -z "$APP" ]; then
  echo "Pakai: $0 /path/ke/folder/app   (folder yang berisi app.yaml)"
  exit 1
fi

echo "[supervisor] $(date '+%F %T') mengawasi app: $APP"
echo "[supervisor] CLI: $CLI"

while true; do
  # no-op bila sudah running; menyalakan kembali bila stopped/failed
  "$CLI" app start "$APP" >/tmp/aiot_supervisor.log 2>&1
  sleep 20
done
