# Smart Room UNO Q AI - AirSense AIoT

Repository untuk sistem monitoring kualitas udara berbasis AI-IoT menggunakan Arduino UNO Q, DHT22, MQ135, OLED SSD1306, Python edge runtime, Flask dashboard, PythonAnywhere, BMKG, dan analitik AI.

## Struktur

- `sketch/` dan `firmware/` : kode Arduino/MCU untuk pembacaan sensor dan komunikasi serial.
- `python/` : runtime edge `main.py`, konfigurasi, kalibrasi MQ135, Edge AI, dan pengiriman data ke server.
- `systemd/` : service `aiot.service` agar program berjalan otomatis.
- `server/` : aplikasi Flask untuk PythonAnywhere.
- `app.yaml`, `install.sh`, `supervisor.sh` : pendukung instalasi dan runtime.

## Dashboard

- Dashboard: https://febryanferdir.eu.pythonanywhere.com/
- Latest API: https://febryanferdir.eu.pythonanywhere.com/api/latest/UNOQ_Rian
- Insight API: https://febryanferdir.eu.pythonanywhere.com/api/insights/UNOQ_Rian
- Export CSV 5 menit: https://febryanferdir.eu.pythonanywhere.com/api/export_5min/UNOQ_Rian

## Catatan

MQ135 digunakan sebagai estimasi CO2e/indikator kualitas udara, bukan sensor CO2 NDIR. Nilai utama dianalisis berdasarkan tren, stabilitas, dan risk score AI.
