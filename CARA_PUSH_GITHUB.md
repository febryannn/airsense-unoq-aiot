# Cara Push Proyek ke GitHub

Repository tujuan:

https://github.com/febryannn/smart-room-unoq-ai

## Opsi A - Push dari folder kode di ZIP ini

Setelah ZIP diekstrak, masuk ke folder:

```bash
cd kode_program/smart-room-unoq-ai
```

Lalu jalankan:

```bash
git init
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/febryannn/smart-room-unoq-ai.git

git add .
git commit -m "Update AirSense AIoT smart room UNO Q system"
git push -u origin main
```

Jika repository GitHub sudah berisi file lama dan push ditolak, gunakan:

```bash
git pull origin main --allow-unrelated-histories
git push -u origin main
```

Kalau tetap ditolak karena history berbeda dan kamu yakin ingin menimpa isi repo dengan versi ini:

```bash
git push -u origin main --force
```

## Opsi B - Push langsung dari Arduino UNO Q

```bash
cd ~/ArduinoApps/tubes-iot-febryan
mkdir -p server laporan

# salin flask_app.py dari PythonAnywhere/server ke folder server jika belum ada
# salin PDF/LaTeX laporan ke folder laporan jika ingin dimasukkan repo

git init
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/febryannn/smart-room-unoq-ai.git

git add README.md app.yaml install.sh supervisor.sh firmware sketch python systemd server laporan .gitignore
git commit -m "Update AirSense AIoT system with dashboard and AI analysis"
git push -u origin main
```

## File yang jangan di-push

- `logs/`
- `__pycache__/`
- `*.pyc`
- `data/state.json`
- database lokal `*.db`
- file sementara LaTeX seperti `*.aux`, `*.log`, `*.toc`
