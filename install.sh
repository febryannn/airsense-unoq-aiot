#!/bin/bash
set -e

PROJECT_DIR="/home/arduino/ArduinoApps/tubes-iot-febryan"
VENV_DIR="/home/arduino/aiot-venv"

if [ "$(pwd)" != "$PROJECT_DIR" ]; then
  echo "Jalankan install.sh dari folder proyek: $PROJECT_DIR"
  echo "Current directory: $(pwd)"
  exit 1
fi

if [ ! -f "python/main.py" ] || [ ! -f "python/requirements.txt" ]; then
  echo "File python/main.py atau python/requirements.txt tidak ditemukan."
  exit 1
fi

echo "[install] Membuat virtualenv di $VENV_DIR"
python3 -m venv "$VENV_DIR"

echo "[install] Upgrade pip dasar"
"$VENV_DIR/bin/python" -m pip install --upgrade pip

echo "[install] Install requirements minimal"
"$VENV_DIR/bin/pip" install -r python/requirements.txt

echo "[install] Membuat folder data dan logs"
mkdir -p data logs

if [ ! -f python/config.json ]; then
  echo "[install] Membuat python/config.json dari contoh"
  cp python/config.example.json python/config.json
else
  echo "[install] python/config.json sudah ada, tidak ditimpa"
fi

cat <<MSG

Manual run:
  $VENV_DIR/bin/python python/main.py

Cek port serial:
  ls /dev/tty*
  dmesg | grep tty

Cek log file:
  tail -f logs/aiot.log
MSG

printf "\nInstall dan start systemd service sekarang? [y/N] "
read -r answer
case "$answer" in
  y|Y|yes|YES)
    echo "[install] Install systemd service aiot"
    sudo cp systemd/aiot.service /etc/systemd/system/aiot.service
    sudo systemctl daemon-reload
    sudo systemctl enable aiot
    sudo systemctl start aiot
    echo "[install] Service aktif. Cek log:"
    echo "  journalctl -u aiot -f"
    ;;
  *)
    echo "[install] Systemd service belum diinstall. Jalankan manual dulu sampai stabil."
    echo "Untuk install nanti: sudo cp systemd/aiot.service /etc/systemd/system/aiot.service"
    ;;
esac
