# ============================================================
#  AI IoT - Server Flask (PythonAnywhere)
#  AirSense AIoT Dashboard Final
#  Device: Arduino UNO Q + DHT22 + MQ135 + OLED SSD1306
#
#  Deploy:
#  1) Upload / paste file ini ke: ~/mysite/flask_app.py
#  2) PythonAnywhere Web tab -> Reload
#  3) Test:
#     /api/latest
#     /api/insights/UNOQ_Rian
# ============================================================

import csv
import io
import json
import math
import os
import sqlite3
import urllib.request
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# -------------------- Konfigurasi --------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "iot_data.db")

WIB = timezone(timedelta(hours=7))
DEVICE = "UNOQ_Rian"
DEVICE_LOCATION = "Kosan Telkom Tirtawangi"
DEVICE_LAT = -6.975071664576171
DEVICE_LON = 107.65186261355015

# Kode ADM4 BMKG untuk Desa Lengkong, Kecamatan Bojongsoang, Kabupaten Bandung.
# Data BMKG dipakai sebagai prakiraan cuaca luar ruangan, sedangkan DHT22 tetap
# dipakai sebagai sensor suhu dan kelembapan ruangan secara realtime.
BMKG_ADM4 = "32.04.08.2001"
BMKG_SOURCE = "BMKG"
BMKG_CACHE = {"ts": 0.0, "data": None}
BMKG_CACHE_SEC = 600

CO2_WARN = 1000.0
CO2_FLOOR = 350.0
FORECAST_STEPS = 10

# Disiapkan untuk LSTM nanti. Jika board mengirim proj10 + pred_src,
# Flask akan menampilkan sumber prediksi dari board.
LATEST_PRED = {}


# ============================================================
#  Utilitas
# ============================================================
def now_wib():
    return datetime.now(WIB)


def now_str():
    return now_wib().strftime("%Y-%m-%d %H:%M:%S")


def as_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isfinite(x):
            return x
        return default
    except Exception:
        return default


def as_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def pick(data, *names, **kwargs):
    default = kwargs.get("default")
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return default


def safe_round(value, ndigits=1, default=None):
    try:
        if value is None:
            return default
        return round(float(value), ndigits)
    except Exception:
        return default


def parse_time(value):
    """Terima format umum dari board/server dan kembalikan datetime naive WIB."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            pass
    return None


def minutes_between(a, b):
    da = parse_time(a)
    db = parse_time(b)
    if da is None or db is None:
        return None
    diff = (db - da).total_seconds() / 60.0
    if diff <= 0:
        return None
    return diff


def coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# ============================================================
#  Database
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, ddl):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT,
            suhu REAL,
            kelembapan REAL,
            co2 REAL,
            kategori TEXT,
            created_at TEXT
        )
    """)
    # Migrasi aman untuk database lama.
    extra_cols = {
        "gas_raw": "INTEGER",
        "raw_ppm": "REAL",
        "trend": "TEXT",
        "rate": "REAL",
        "anomaly": "INTEGER DEFAULT 0",
        "z_score": "REAL",
        "risk": "TEXT",
        "urgency": "TEXT",
        "recommendation": "TEXT",
        "comfort": "TEXT",
        "dht_ok": "INTEGER",
        "seq": "INTEGER",
        "mcu_millis": "INTEGER",
        "proj10": "REAL",
        "pred_src": "TEXT",
        "payload_json": "TEXT",
    }
    for col, ddl in extra_cols.items():
        ensure_column(conn, "readings", col, ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_device_id ON readings(device, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_created ON readings(created_at)")
    conn.commit()
    conn.close()


init_db()


# ============================================================
#  AI Engine
# ============================================================
def classify_air(ppm):
    ppm = as_float(ppm)
    if ppm is None:
        return "-", "#8E8E93", "Menunggu data sensor."
    if ppm < 600:
        return "Baik", "#34C759", "Udara aman. Pertahankan ventilasi normal."
    if ppm < 1000:
        return "Sedang", "#FFCC00", "Masih wajar, tetapi sirkulasi udara perlu dijaga."
    if ppm < 1500:
        return "Tidak Sehat", "#FF9500", "Buka jendela atau tingkatkan ventilasi."
    if ppm < 2500:
        return "Sangat Tidak Sehat", "#FF3B30", "Nyalakan exhaust dan kurangi aktivitas di ruangan."
    return "Berbahaya", "#C70000", "Segera keluar/ventilasi ruangan dan periksa sumber polusi."


def air_level_index(ppm):
    ppm = as_float(ppm)
    if ppm is None:
        return 0
    if ppm < 600:
        return 0
    if ppm < 1000:
        return 1
    if ppm < 1500:
        return 2
    if ppm < 2500:
        return 3
    return 4


def moving_average(values, window=5):
    out = []
    for i in range(len(values)):
        seg = values[max(0, i - window + 1): i + 1]
        out.append(round(sum(seg) / len(seg), 1))
    return out


def linear_fit(values):
    n = len(values)
    if n < 3:
        return 0.0, values[-1] if values else 0.0, 0.0
    xs = list(range(n))
    sx = sum(xs)
    sy = sum(values)
    sxy = sum(x * y for x, y in zip(xs, values))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return 0.0, values[-1], 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    mean = sy / n
    ss_tot = sum((y - mean) ** 2 for y in values)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, values))
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    return slope, intercept, round(max(0.0, min(1.0, r2)), 2)


def estimate_step_minutes(rows):
    if len(rows) < 2:
        return 1.0
    intervals = []
    tail = rows[-20:]
    for a, b in zip(tail[:-1], tail[1:]):
        m = minutes_between(a.get("created_at"), b.get("created_at"))
        if m is not None and 0.05 <= m <= 30:
            intervals.append(m)
    if not intervals:
        return 1.0
    intervals.sort()
    return intervals[len(intervals) // 2]


def forecast(values, steps=FORECAST_STEPS):
    recent = values[-20:] if len(values) > 20 else values
    slope, intercept, r2 = linear_fit(recent)
    n = len(recent)
    fut = []
    for k in range(1, steps + 1):
        pred = slope * (n - 1 + k) + intercept
        fut.append(max(CO2_FLOOR, round(pred, 1)))
    return fut, slope, r2


def trend_label(rate_per_min):
    if rate_per_min > 15:
        return "Memburuk Cepat"
    if rate_per_min > 1.5:
        return "Memburuk"
    if rate_per_min < -15:
        return "Membaik Cepat"
    if rate_per_min < -1.5:
        return "Membaik"
    return "Stabil"


def anomaly_flags(values, k=2.5):
    flags = [False] * len(values)
    if len(values) < 8:
        return flags, 0.0
    mean = sum(values) / len(values)
    std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    if std < 1e-6:
        return flags, 0.0
    for i, value in enumerate(values):
        flags[i] = abs((value - mean) / std) > k
    return flags, round((values[-1] - mean) / std, 2)


def eta_threshold(current, rate_per_min, threshold=CO2_WARN):
    if current is None:
        return None
    if current >= threshold:
        return 0
    if rate_per_min is None or rate_per_min <= 0.1:
        return None
    return int(round((threshold - current) / rate_per_min))


def comfort_index(t, h):
    t = as_float(t)
    h = as_float(h)
    if t is None or h is None:
        return "-"
    if t < 26 and 40 <= h <= 60:
        return "Nyaman"
    if t >= 32 or h > 75:
        return "Gerah / Lembap"
    if t < 20:
        return "Dingin"
    if h > 65:
        return "Cukup Lembap"
    return "Cukup Nyaman"


def air_score(co2):
    co2 = as_float(co2)
    if co2 is None:
        return 0
    # 400 ppm = 100, 2000 ppm = 0
    return int(max(0, min(100, round(100 * (1 - (co2 - 400) / 1600)))))


def risk_score(co2, rate_per_min, temp, hum):
    co2 = as_float(co2)
    rate_per_min = as_float(rate_per_min, 0.0) or 0.0
    temp = as_float(temp)
    hum = as_float(hum)
    if co2 is None:
        return 0, "-", "#8E8E93"
    base = max(0.0, min(75.0, (co2 - 400.0) / 2100.0 * 75.0))
    trend_pen = max(0.0, min(20.0, rate_per_min * 0.9))
    env_pen = 0.0
    if temp is not None and temp >= 32:
        env_pen += 5.0
    if hum is not None and hum > 75:
        env_pen += 5.0
    score = int(round(max(0.0, min(100.0, base + trend_pen + env_pen))))
    if score <= 25:
        return score, "Aman", "#34C759"
    if score <= 50:
        return score, "Perlu Perhatian", "#FFCC00"
    if score <= 75:
        return score, "Tinggi", "#FF9500"
    return score, "Kritis", "#FF3B30"


def ventilation_score(co2, rate_per_min):
    co2 = as_float(co2)
    rate_per_min = as_float(rate_per_min, 0.0) or 0.0
    if co2 is None:
        return 0, "-", "#8E8E93"
    level_pen = max(0.0, min(65.0, (co2 - 415.0) / 1200.0 * 65.0))
    rise_pen = max(0.0, min(35.0, rate_per_min * 1.4))
    score = int(round(max(0.0, min(100.0, 100.0 - level_pen - rise_pen))))
    if score >= 75:
        return score, "Baik", "#34C759"
    if score >= 45:
        return score, "Cukup", "#FFCC00"
    return score, "Buruk", "#FF9500"


def accumulation_label(rate_per_min):
    rate_per_min = as_float(rate_per_min, 0.0) or 0.0
    if rate_per_min < -10:
        return "Menurun cepat"
    if rate_per_min < -1.5:
        return "Menurun"
    if rate_per_min <= 1.5:
        return "Stabil"
    if rate_per_min <= 10:
        return "Naik sedang"
    if rate_per_min <= 30:
        return "Naik cepat"
    return "Sangat cepat"


def decide(co2, trend, anomaly, eta):
    co2 = as_float(co2)
    if co2 is None:
        return "Menunggu data sensor", "-", "#8E8E93"
    if co2 >= 2500:
        return "Evakuasi area, buka semua ventilasi, periksa sumber polusi.", "Kritis", "#C70000"
    if co2 >= 1500:
        return "Nyalakan exhaust/ventilasi kuat sekarang.", "Tinggi", "#FF9500"
    if eta is not None and 0 <= eta <= 10:
        return "CO2 menuju ambang. Nyalakan ventilasi sebelum melewati 1000 ppm.", "Tinggi", "#FF9500"
    if co2 >= 1000:
        return "Buka jendela dan tingkatkan sirkulasi udara.", "Sedang", "#FFCC00"
    if trend in ("Memburuk", "Memburuk Cepat"):
        return "CO2 mulai naik. Pantau ruangan dan siapkan ventilasi.", "Perhatian", "#007AFF"
    if anomaly:
        return "Perubahan mendadak terdeteksi. Periksa sumber gas atau posisi sensor.", "Perhatian", "#007AFF"
    return "Kondisi aman. Pertahankan ventilasi normal.", "Aman", "#34C759"


def oled_lines(latest, ai):
    """Mirror tampilan OLED hardware: Suhu, RH, Gas, CO2."""
    if not latest:
        return ["AIoT      --:--:--", "Suhu : --.- C", "RH   : --.- %", "Gas  : ----", "CO2  : ---- ppm"]
    created = latest.get("created_at") or now_str()
    time_txt = created[11:19] if len(created) >= 19 else now_wib().strftime("%H:%M:%S")
    suhu = safe_round(latest.get("suhu"), 1, "--")
    hum = safe_round(latest.get("kelembapan"), 1, "--")
    gas = latest.get("gas_raw")
    gas_txt = str(int(gas)) if gas is not None else "----"
    co2_val = as_float(latest.get("co2"))
    co2_txt = str(int(round(co2_val))) if co2_val is not None else "----"
    return [
        "AIoT      %s" % time_txt,
        "Suhu : %s C" % suhu,
        "RH   : %s %%" % hum,
        "Gas  : %s" % gas_txt,
        "CO2  : %s ppm" % co2_txt,
    ]


# ============================================================
#  Data access
# ============================================================
def row_to_dict(row):
    d = dict(row)

    # Generic merge: semua field di payload_json dijadikan top-level.
    # Ini membuat Edge AI, Occupancy, dan DHT22 LSTM langsung terbaca dashboard/API.
    try:
        _payload = d.get("payload_json")
        if _payload:
            _extra = json.loads(_payload)
            if isinstance(_extra, dict):
                for _k, _v in _extra.items():
                    if _k not in d or d.get(_k) is None or d.get(_k) == "":
                        d[_k] = _v
    except Exception:
        pass

    # Expose field tambahan dari payload_json:
    # Edge AI, Occupancy, dan DHT22 LSTM.
    try:
        _payload = d.get("payload_json")
        if _payload:
            _extra = json.loads(_payload)
            for _k in (
                "edge_ai_mode",
                "ai_risk_score",
                "ai_risk_level",
                "ai_cause",
                "ai_forecast_5m",
                "ai_forecast_10m",
                "ai_forecast_5m_level",
                "ai_forecast_10m_level",
                "ai_eta_1000_min",
                "ai_action",
                "ai_urgency",
                "ai_confidence",
                "ai_summary",
                "occupancy",
                "occupancy_prob",
                "occupancy_percent",
                "occupancy_label",
                "occupancy_ai",
                "prediksi_5m",
                "prediksi_10m",

                # DHT22 LSTM
                "dht22_lstm_mode",
                "dht22_lstm_station_id",
                "dht22_lstm_seq_len",
                "dht22_lstm_pred_days",
                "dht22_lstm_last_date",
                "dht22_lstm_pred_date",
                "dht22_lstm_pred_temp",
                "dht22_lstm_pred_humidity",
                "dht22_lstm_temp_mae",
                "dht22_lstm_rh_mae",
                "dht22_lstm_temp_r2",
                "dht22_lstm_rh_r2",
                "dht22_lstm_comfort",
                "dht22_lstm_summary",
                "dht22_realtime_temp",
                "dht22_realtime_humidity",
                "dht22_realtime_comfort",
            ):
                if _k in _extra:
                    d[_k] = _extra.get(_k)
    except Exception:
        pass

    # Expose Edge AI / Occupancy fields from payload_json without DB migration.
    try:
        _payload = d.get("payload_json")
        if _payload:
            _extra = json.loads(_payload)
            for _k in (
                "edge_ai_mode",
                "ai_risk_score",
                "ai_risk_level",
                "ai_cause",
                "ai_forecast_5m",
                "ai_forecast_10m",
                "ai_forecast_5m_level",
                "ai_forecast_10m_level",
                "ai_eta_1000_min",
                "ai_action",
                "ai_urgency",
                "ai_confidence",
                "ai_summary",
                "ai_error",
                "occupancy",
                "occupancy_prob",
                "occupancy_percent",
                "occupancy_label",
                "occupancy_ai",
                "prediksi_5m",
                "prediksi_10m",
            ):
                if _k in _extra:
                    d[_k] = _extra.get(_k)
    except Exception:
        pass
    for key in ("suhu", "kelembapan", "co2", "raw_ppm", "rate", "z_score", "proj10"):
        if key in d and d[key] is not None:
            d[key] = safe_round(d[key], 2)
    for key in ("gas_raw", "seq", "mcu_millis", "anomaly", "dht_ok"):
        if key in d and d[key] is not None:
            d[key] = int(d[key])
    return d


def get_rows(device=DEVICE, limit=500, newest_first=False):
    device = device or DEVICE
    limit = max(1, min(as_int(limit, 500) or 500, 100000))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM readings WHERE device=? ORDER BY id DESC LIMIT ?",
        (device, limit),
    ).fetchall()
    conn.close()
    data = [row_to_dict(r) for r in rows]
    return data if newest_first else list(reversed(data))


def get_latest(device=DEVICE):
    rows = get_rows(device, 1, newest_first=True)
    return rows[0] if rows else None


# ============================================================
#  API
# ============================================================
@app.route("/api/post/data", methods=["POST"])
def api_post():
    d = request.get_json(force=True, silent=True) or {}

    device = pick(d, "device", "device_name", default=DEVICE)
    created = pick(d, "created_at", "datetime", "timestamp")
    if not created:
        created = now_str()

    suhu = as_float(pick(d, "suhu", "temp", "temperature"))
    kelembapan = as_float(pick(d, "kelembapan", "hum", "humidity", "rh"))
    co2 = as_float(pick(d, "co2", "ppm", "co2_ppm"))
    kategori = pick(d, "kategori", "air_quality", "quality")
    if kategori is None and co2 is not None:
        kategori = classify_air(co2)[0]

    trend = pick(d, "trend", "tren")
    rate = as_float(pick(d, "rate", "laju", "rate_per_min", "rate_ppm_per_min"))
    anomaly = coerce_bool(pick(d, "anomaly", "anomali"))
    z_score = as_float(pick(d, "z_score", "z", "zscore"))
    gas_raw = as_int(pick(d, "gas_raw", "raw", "mq135_raw"))
    raw_ppm = as_float(pick(d, "raw_ppm", "ppm_raw"))
    urgency = pick(d, "urgency", "tingkat_urgensi", "urgensi")
    recommendation = pick(d, "recommendation", "keputusan", "aksi", "rekomendasi")
    comfort = pick(d, "comfort", "kenyamanan")
    dht_ok = coerce_bool(pick(d, "dht_ok"))
    seq = as_int(pick(d, "seq"))
    mcu_millis = as_int(pick(d, "mcu_millis", "millis"))
    proj10 = as_float(pick(d, "proj10", "prediksi10", "projection_10m", "proyeksi10_lstm"))
    pred_src = pick(d, "pred_src", "prediction_source")
    if proj10 is not None:
        pred_src = pred_src or "LSTM"
        LATEST_PRED[device] = {"proj10": proj10, "src": pred_src}

    conn = get_db()
    conn.execute(
        """
        INSERT INTO readings (
            device, suhu, kelembapan, co2, kategori, created_at,
            gas_raw, raw_ppm, trend, rate, anomaly, z_score,
            risk, urgency, recommendation, comfort, dht_ok, seq,
            mcu_millis, proj10, pred_src, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device, suhu, kelembapan, co2, kategori, created,
            gas_raw, raw_ppm, trend, rate, int(bool(anomaly)) if anomaly is not None else 0,
            z_score, None, urgency, recommendation, comfort,
            int(dht_ok) if dht_ok is not None else None, seq, mcu_millis,
            proj10, pred_src, json.dumps(d, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message": "Data inserted successfully",
        "saved": {
            "device": device,
            "suhu": suhu,
            "kelembapan": kelembapan,
            "co2": co2,
            "kategori": kategori,
            "created_at": created,
            "proj10": proj10,
            "pred_src": pred_src,
        }
    })


@app.route("/api/latest", methods=["GET"])
def api_latest_default():
    return api_latest(DEVICE)


@app.route("/api/latest/<device>", methods=["GET"])
def api_latest(device):
    latest = get_latest(device)
    if not latest:
        return jsonify({"ok": False, "latest": None, "message": "Belum ada data."}), 404
    return jsonify({"ok": True, "latest": latest})


@app.route("/api/history", methods=["GET"])
def api_history_default():
    return api_history(DEVICE)


@app.route("/api/history/<device>", methods=["GET"])
def api_history(device):
    limit = as_int(request.args.get("limit"), 500) or 500
    return jsonify({"ok": True, "device": device, "data": get_rows(device, limit)})


@app.route("/api/get/data/<device>", methods=["GET"])
def api_get(device):
    if "last_data" in request.args:
        return jsonify(get_rows(device, 1, newest_first=True))
    if "h2" in request.args:
        return jsonify(get_rows(device, 120))
    limit = as_int(request.args.get("limit"), 500) or 500
    return jsonify(get_rows(device, limit))


@app.route("/api/export/<device>", methods=["GET"])
def api_export(device):
    rows = get_rows(device, 100000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "created_at", "device", "suhu", "kelembapan", "co2", "kategori",
        "trend", "rate", "gas_raw", "raw_ppm", "anomaly", "dht_ok",
        "proj10", "pred_src"
    ])
    for r in rows:
        writer.writerow([
            r.get("created_at"), r.get("device"), r.get("suhu"), r.get("kelembapan"),
            r.get("co2"), r.get("kategori"), r.get("trend"), r.get("rate"),
            r.get("gas_raw"), r.get("raw_ppm"), r.get("anomaly"), r.get("dht_ok"),
            r.get("proj10"), r.get("pred_src")
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=%s_data.csv" % device}
    )


@app.route("/api/reset/<device>", methods=["GET", "POST"])
def api_reset(device):
    if request.args.get("confirm") != "RESET":
        return jsonify({"ok": False, "error": "Tambahkan ?confirm=RESET untuk menghapus data."}), 400
    conn = get_db()
    if device == "all":
        n = conn.execute("DELETE FROM readings").rowcount
        LATEST_PRED.clear()
    else:
        n = conn.execute("DELETE FROM readings WHERE device=?", (device,)).rowcount
        LATEST_PRED.pop(device, None)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Data direset.", "baris_terhapus": n})


@app.route("/api/health", methods=["GET"])
def api_health():
    latest = get_latest(DEVICE)
    return jsonify({
        "ok": True,
        "device": DEVICE,
        "server_time_wib": now_str(),
        "latest": latest,
        "db_path": DB_PATH,
    })


@app.route("/api/oled/<device>", methods=["GET"])
def api_oled(device):
    insights = build_insights(device)
    return jsonify({
        "ok": True,
        "device": device,
        "oled": insights.get("oled"),
        "latest": insights.get("latest"),
    })



def _flatten_bmkg_cuaca(cuaca):
    """Flatten struktur cuaca BMKG yang bisa berupa list bertingkat."""
    out = []
    if isinstance(cuaca, list):
        for item in cuaca:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, list):
                out.extend(_flatten_bmkg_cuaca(item))
    return out


def fetch_bmkg_weather():
    """
    Ambil prakiraan cuaca BMKG berdasarkan kode ADM4.

    Catatan:
    - BMKG = prakiraan cuaca luar ruangan.
    - DHT22 = suhu dan kelembapan ruangan realtime.
    - Dashboard menampilkan keduanya agar prediksi tidak disamakan dengan suhu kamar.
    """
    now_ts = time.time()
    if BMKG_CACHE.get("data") is not None and now_ts - BMKG_CACHE.get("ts", 0) < BMKG_CACHE_SEC:
        return BMKG_CACHE["data"]

    url = "https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4=%s" % BMKG_ADM4

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AirSense-AIoT-Febryan/1.0"}
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", errors="replace")

        data = json.loads(raw)
        lokasi = data.get("lokasi") or {}

        data_list = data.get("data") or []
        cuaca_raw = []
        if data_list and isinstance(data_list[0], dict):
            cuaca_raw = data_list[0].get("cuaca") or []
        flat = _flatten_bmkg_cuaca(cuaca_raw)

        first = flat[0] if flat else {}

        current = {
            "local_datetime": first.get("local_datetime") or first.get("datetime"),
            "temp": as_float(first.get("t")),
            "humidity": as_float(first.get("hu")),
            "weather": first.get("weather_desc") or first.get("weather"),
            "weather_en": first.get("weather_desc_en"),
            "wind_speed": as_float(first.get("ws")),
            "wind_dir": first.get("wd"),
            "cloud_cover": as_float(first.get("tcc")),
            "visibility": first.get("vs_text"),
            "image": first.get("image"),
        }

        result = {
            "ok": True,
            "source": BMKG_SOURCE,
            "adm4": BMKG_ADM4,
            "url": url,
            "lokasi": {
                "desa": lokasi.get("desa"),
                "kecamatan": lokasi.get("kecamatan"),
                "kotkab": lokasi.get("kotkab"),
                "provinsi": lokasi.get("provinsi"),
                "lat": lokasi.get("lat"),
                "lon": lokasi.get("lon"),
                "timezone": lokasi.get("timezone"),
            },
            "current": current,
            "forecast": flat[:8],
            "note": "Prakiraan cuaca luar ruangan dari BMKG; bukan pembacaan suhu ruangan DHT22.",
        }

        BMKG_CACHE["ts"] = now_ts
        BMKG_CACHE["data"] = result
        return result

    except Exception as e:
        return {
            "ok": False,
            "source": BMKG_SOURCE,
            "adm4": BMKG_ADM4,
            "url": url,
            "error": str(e),
        }


@app.route("/api/weather/bmkg", methods=["GET"])
def api_weather_bmkg():
    return jsonify(fetch_bmkg_weather())




def compact_recent_rows(rows, limit=6):
    """
    Ambil data terbaru untuk tabel dashboard, tetapi hilangkan baris duplikat.
    Data mentah tetap tersimpan lengkap di database dan CSV.
    """
    out = []
    seen = set()
    for r in reversed(rows):
        key = (
            r.get("created_at"), r.get("suhu"), r.get("kelembapan"),
            r.get("co2"), r.get("kategori"), r.get("trend"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def compact_chart_rows(rows, minute_bucket=True, max_points=90):
    """
    Ringkas data untuk grafik dashboard.
    Data mentah tetap tersimpan lengkap, tetapi grafik memakai rata-rata per menit
    supaya grafik outdoor 3 jam tetap rapi dan mudah dibaca.
    """
    if not rows:
        return []
    if not minute_bucket:
        return rows[-max_points:]

    buckets = {}
    for r in rows:
        ts = r.get("created_at") or ""
        key = ts[:16] if len(ts) >= 16 else ts
        if not key:
            continue
        if key not in buckets:
            buckets[key] = {
                "created_at": key + ":00",
                "device": r.get("device"),
                "suhu_values": [],
                "hum_values": [],
                "co2_values": [],
                "gas_values": [],
                "kategori": r.get("kategori"),
                "trend": r.get("trend"),
            }

        def add_num(name, value):
            try:
                if value is not None and value != "":
                    buckets[key][name].append(float(value))
            except Exception:
                pass

        add_num("suhu_values", r.get("suhu"))
        add_num("hum_values", r.get("kelembapan"))
        add_num("co2_values", r.get("co2"))
        add_num("gas_values", r.get("gas_raw"))
        if r.get("kategori"):
            buckets[key]["kategori"] = r.get("kategori")
        if r.get("trend"):
            buckets[key]["trend"] = r.get("trend")

    out = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        def avg(vals, nd=1):
            if not vals:
                return None
            return round(sum(vals) / len(vals), nd)
        out.append({
            "created_at": b["created_at"],
            "device": b.get("device"),
            "suhu": avg(b["suhu_values"], 1),
            "kelembapan": avg(b["hum_values"], 1),
            "co2": avg(b["co2_values"], 1),
            "gas_raw": int(round(avg(b["gas_values"], 0))) if b["gas_values"] else None,
            "kategori": b.get("kategori"),
            "trend": b.get("trend"),
        })
    return out[-max_points:]


def build_insights(device=DEVICE):
    hist = get_rows(device, as_int(request.args.get("limit"), 240) or 240)
    if not hist:
        return {
            "ok": False,
            "device": device,
            "latest": None,
            "ai": {},
            "chart": {"labels": [], "co2": [], "ma": [], "suhu": [], "hum": [], "anomaly": [], "forecast_labels": [], "forecast": []},
            "recent": [],
            "oled": oled_lines(None, {}),
        }

    # Filter data valid untuk chart sambil mempertahankan waktu.
    valid = [r for r in hist if as_float(r.get("co2")) is not None]
    if not valid:
        latest = hist[-1]
        return {
            "ok": False,
            "device": device,
            "latest": latest,
            "ai": {},
            "chart": {"labels": [], "co2": [], "ma": [], "suhu": [], "hum": [], "anomaly": [], "forecast_labels": [], "forecast": []},
            "recent": compact_recent_rows(hist, 6),
            "oled": oled_lines(latest, {}),
        }

    # Data valid asli dipakai untuk analisis statistik.
    co2_raw = [float(r.get("co2")) for r in valid]

    # Data grafik diringkas per menit agar dashboard outdoor tidak terlalu padat.
    chart_valid = compact_chart_rows(valid, minute_bucket=True, max_points=90)
    if not chart_valid:
        chart_valid = valid[-90:]

    co2 = [float(r.get("co2")) for r in chart_valid if as_float(r.get("co2")) is not None]
    suhu = [as_float(r.get("suhu")) for r in chart_valid]
    hum = [as_float(r.get("kelembapan")) for r in chart_valid]
    labels = [(r.get("created_at") or "")[11:16] for r in chart_valid]

    latest = valid[-1]
    cur = float(latest.get("co2"))

    step_min = estimate_step_minutes(valid)
    ma = moving_average(co2, 5)
    fut, slope_per_sample, r2 = forecast(co2, FORECAST_STEPS)
    rate_per_min = slope_per_sample / step_min if step_min > 0 else slope_per_sample
    eta = eta_threshold(cur, rate_per_min, CO2_WARN)
    flags, z = anomaly_flags(co2)
    trend = trend_label(rate_per_min)
    anomaly_now = bool(flags[-1]) if flags else False

    kategori, warna, saran = classify_air(cur)
    risk, risk_cat, risk_warna = risk_score(cur, rate_per_min, latest.get("suhu"), latest.get("kelembapan"))
    vent, vent_status, vent_warna = ventilation_score(cur, rate_per_min)
    score = air_score(cur)
    comfort = comfort_index(latest.get("suhu"), latest.get("kelembapan"))
    aksi, tingkat, aksi_warna = decide(cur, trend, anomaly_now, eta)

    mean_co2 = sum(co2_raw) / len(co2_raw)
    std = (sum((v - mean_co2) ** 2 for v in co2_raw) / len(co2_raw)) ** 0.5 if co2 else 0.0
    anom_pct = int(round((cur - mean_co2) / mean_co2 * 100)) if anomaly_now and mean_co2 > 0 else 0

    # Prediksi dari board/LSTM jika tersedia; prioritas row terbaru, lalu cache.
    p10 = latest.get("proj10")
    p10_src = latest.get("pred_src")
    if p10 is None:
        cached = LATEST_PRED.get(device)
        if cached:
            p10 = cached.get("proj10")
            p10_src = cached.get("src")
    if p10 is None:
        p10 = fut[-1] if fut else cur
        p10_src = "linear"
    p10 = safe_round(p10, 1, cur)
    p10_cat = classify_air(p10)[0]

    created_dt = parse_time(latest.get("created_at")) or now_wib().replace(tzinfo=None)
    future_labels = []
    for k in range(1, FORECAST_STEPS + 1):
        future_labels.append((created_dt + timedelta(minutes=step_min * k)).strftime("%H:%M"))

    ai = {
        "analisis": {
            "avg": round(mean_co2, 1),
            "min": round(min(co2_raw), 1),
            "max": round(max(co2_raw), 1),
            "std": round(std, 1),
            "n": len(valid),
            "step_menit": round(step_min, 2),
            "laju": round(rate_per_min, 2),
        },
        "prediksi": {
            "next": fut[0] if fut else cur,
            "slope": round(rate_per_min, 2),
            "tren": trend,
            "eta_menit": eta,
            "r2": r2,
            "ambang": CO2_WARN,
        },
        "klasifikasi": {"kategori": kategori, "warna": warna, "saran": saran},
        "anomali": {"now": anomaly_now, "z": z, "persen": anom_pct},
        "skor": score,
        "risiko": {"skor": risk, "kategori": risk_cat, "warna": risk_warna},
        "ventilasi": {"skor": vent, "status": vent_status, "warna": vent_warna},
        "proyeksi10": {"ppm": p10, "kategori": p10_cat, "src": p10_src or "linear"},
        "akumulasi": {"laju": round(rate_per_min, 2), "label": accumulation_label(rate_per_min)},
        "otomasi": {
            "aktif": bool(cur >= 1000 or (eta is not None and 0 <= eta <= 10)),
            "durasi": 15 if cur >= 1000 else (10 if eta is not None and 0 <= eta <= 10 else 0),
        },
        "kenyamanan": comfort,
        "keputusan": {"aksi": aksi, "tingkat": tingkat, "warna": aksi_warna},
    }

    # Sinkronisasi nilai ringkas agar latest juga sesuai OLED/dashboard.
    latest = dict(latest)
    latest["kategori"] = kategori
    latest["trend"] = trend
    latest["rate"] = round(rate_per_min, 2)
    latest["comfort"] = comfort

    # Prefer Edge AI values from the device when available.
    if latest.get("edge_ai_mode"):
        if latest.get("ai_risk_score") is not None:
            ai["risiko"] = {
                "skor": latest.get("ai_risk_score"),
                "kategori": latest.get("ai_risk_level") or risk_cat,
                "warna": risk_warna,
            }
        if latest.get("ai_forecast_10m") is not None:
            ai["proyeksi10"] = {
                "ppm": latest.get("ai_forecast_10m"),
                "kategori": latest.get("ai_forecast_10m_level") or p10_cat,
                "src": "Hybrid Air Quality Risk",
            }
        if latest.get("ai_action"):
            ai["keputusan"] = {
                "aksi": latest.get("ai_action"),
                "tingkat": latest.get("ai_urgency") or tingkat,
                "warna": aksi_warna,
            }
        if latest.get("ai_cause"):
            ai["akumulasi"] = {
                "laju": round(rate_per_min, 2),
                "label": latest.get("ai_cause"),
            }
        if latest.get("ai_confidence") is not None:
            ai["confidence"] = latest.get("ai_confidence")

    return {
        "ok": True,
        "device": device,
        "server_time_wib": now_str(),
        "latest": latest,
        "ai": ai,
        "bmkg": fetch_bmkg_weather(),
        "oled": oled_lines(latest, ai),
        "chart": {
            "labels": labels,
            "co2": co2,
            "ma": ma,
            "suhu": suhu,
            "hum": hum,
            "anomaly": flags,
            "forecast_labels": future_labels,
            "forecast": fut,
        },
        "recent": compact_recent_rows(hist, 6),
    }


@app.route("/api/insights/<device>", methods=["GET"])
def api_insights(device):
    return jsonify(build_insights(device))


# ============================================================
#  Ilustrasi perangkat (SVG vektor - tajam di resolusi apa pun)
# ============================================================
SVG_UNOQ = '''<svg viewBox="0 0 150 110" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="uq" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#16b8a6"/><stop offset="1" stop-color="#0c8f86"/></linearGradient></defs>
<rect x="16" y="24" width="118" height="62" rx="8" fill="url(#uq)" stroke="#0a6f68" stroke-width="2"/>
<rect x="8" y="44" width="12" height="20" rx="3" fill="#d7dee8" stroke="#aab6c4"/>
<rect x="58" y="40" width="34" height="30" rx="3" fill="#11161f"/><rect x="62" y="44" width="26" height="22" rx="2" fill="#222c3c"/>
<text x="75" y="59" font-family="monospace" font-size="7" fill="#5fd6c9" text-anchor="middle">Q</text>
<rect x="100" y="46" width="18" height="18" rx="2" fill="#11161f"/>
<rect x="24" y="26" width="64" height="6" rx="2" fill="#11161f"/><rect x="24" y="78" width="86" height="6" rx="2" fill="#11161f"/>
<g fill="#ff5a52"><circle cx="106" cy="32" r="2"/><circle cx="113" cy="32" r="2"/><circle cx="120" cy="32" r="2"/></g>
<circle cx="28" cy="40" r="2.5" fill="#0a6f68"/><circle cx="122" cy="78" r="2.5" fill="#0a6f68"/>
</svg>'''

SVG_DHT22 = '''<svg viewBox="0 0 150 110" xmlns="http://www.w3.org/2000/svg">
<rect x="46" y="14" width="58" height="64" rx="8" fill="#f4f7fb" stroke="#c7d2de" stroke-width="2"/>
<g fill="#9aa7b6">
<circle cx="58" cy="26" r="2.4"/><circle cx="68" cy="26" r="2.4"/><circle cx="78" cy="26" r="2.4"/><circle cx="88" cy="26" r="2.4"/>
<circle cx="58" cy="36" r="2.4"/><circle cx="68" cy="36" r="2.4"/><circle cx="78" cy="36" r="2.4"/><circle cx="88" cy="36" r="2.4"/>
<circle cx="58" cy="46" r="2.4"/><circle cx="68" cy="46" r="2.4"/><circle cx="78" cy="46" r="2.4"/><circle cx="88" cy="46" r="2.4"/>
<circle cx="58" cy="56" r="2.4"/><circle cx="68" cy="56" r="2.4"/><circle cx="78" cy="56" r="2.4"/><circle cx="88" cy="56" r="2.4"/>
</g>
<text x="75" y="71" font-family="monospace" font-size="7" fill="#64748b" text-anchor="middle">DHT22</text>
<g stroke="#caa53a" stroke-width="3.5" stroke-linecap="round"><line x1="56" y1="78" x2="56" y2="98"/><line x1="68" y1="78" x2="68" y2="98"/><line x1="82" y1="78" x2="82" y2="98"/><line x1="94" y1="78" x2="94" y2="98"/></g>
</svg>'''

SVG_MQ135 = '''<svg viewBox="0 0 150 110" xmlns="http://www.w3.org/2000/svg">
<rect x="34" y="40" width="82" height="44" rx="6" fill="#1f6fd0" stroke="#1559ab" stroke-width="2"/>
<circle cx="75" cy="42" r="26" fill="#aeb9c6" stroke="#7c8aa0" stroke-width="2"/>
<circle cx="75" cy="42" r="20" fill="#c3cdd9"/>
<g stroke="#8693a6" stroke-width="1.4">
<line x1="57" y1="42" x2="93" y2="42"/><line x1="60" y1="33" x2="90" y2="51"/><line x1="60" y1="51" x2="90" y2="33"/><line x1="75" y1="24" x2="75" y2="60"/>
</g>
<circle cx="75" cy="42" r="6" fill="#6b7686"/>
<g stroke="#caa53a" stroke-width="3" stroke-linecap="round"><line x1="48" y1="84" x2="48" y2="100"/><line x1="61" y1="84" x2="61" y2="100"/><line x1="89" y1="84" x2="89" y2="100"/><line x1="102" y1="84" x2="102" y2="100"/></g>
<text x="100" y="62" font-family="monospace" font-size="6.5" fill="#dfe8f5" text-anchor="middle">MQ135</text>
</svg>'''

SVG_OLED = '''<svg viewBox="0 0 150 110" xmlns="http://www.w3.org/2000/svg">
<rect x="28" y="20" width="94" height="62" rx="6" fill="#0c1422" stroke="#26344a" stroke-width="2"/>
<rect x="38" y="30" width="74" height="40" rx="3" fill="#05101f"/>
<rect x="40" y="32" width="70" height="36" rx="2" fill="#071a33"/>
<g fill="#3aa7ff"><rect x="45" y="37" width="40" height="4" rx="1"/><rect x="45" y="46" width="56" height="3" rx="1"/><rect x="45" y="53" width="48" height="3" rx="1"/><rect x="45" y="60" width="34" height="3" rx="1"/></g>
<g fill="#11161f"><rect x="40" y="14" width="5" height="6" rx="1"/><rect x="50" y="14" width="5" height="6" rx="1"/><rect x="60" y="14" width="5" height="6" rx="1"/><rect x="70" y="14" width="5" height="6" rx="1"/></g>
<text x="75" y="79" font-family="monospace" font-size="6.5" fill="#5b7796" text-anchor="middle">SSD1306</text>
</svg>'''


# ============================================================
#  Dashboard Final
# ============================================================
DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirSense AIoT - Dashboard Kualitas Udara</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#F2F2F7;
  --surface:rgba(255,255,255,.72); --surface2:rgba(255,255,255,.55);
  --hair:rgba(60,60,67,.12); --hair2:rgba(60,60,67,.08);
  --label:#1c1c1e; --label2:rgba(60,60,67,.62); --label3:rgba(60,60,67,.32); --muted:rgba(60,60,67,.62);
  --blue:#007AFF; --teal:#30B0C7; --indigo:#5856D6;
  --green:#34C759; --yellow:#FFCC00; --orange:#FF9500; --red:#FF3B30;
  --fill:rgba(118,118,128,.12);
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,"Roboto Mono",monospace;
  --r:22px;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;font-family:var(--sans);color:var(--label);background:var(--bg);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;text-rendering:optimizeLegibility}
body{overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;z-index:-2;background:
  radial-gradient(820px 520px at 84% -8%,rgba(0,122,255,.16),transparent 62%),
  radial-gradient(720px 520px at 4% 2%,rgba(48,176,199,.15),transparent 60%),
  radial-gradient(760px 620px at 50% 118%,rgba(88,86,214,.12),transparent 62%),
  var(--bg)}
.sg{font-family:var(--sans)} .mono{font-family:var(--mono)}
.wrap{max-width:1280px;margin:0 auto;padding:20px 20px 30px;display:flex;flex-direction:column;gap:16px}
.glass{background:var(--surface);border:1px solid rgba(255,255,255,.6);border-radius:var(--r);box-shadow:0 1px 2px rgba(0,0,0,.04),0 12px 34px rgba(0,0,0,.06);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%)}
.topbar{padding:11px 15px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;position:sticky;top:12px;z-index:30}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:11px;background:linear-gradient(160deg,#3cc6ff,#007AFF 52%,#5856D6);display:grid;place-items:center;color:#fff;font-size:21px;box-shadow:0 4px 13px rgba(0,122,255,.34),inset 0 1px 0 rgba(255,255,255,.55)}
.brand h1{font:600 18px/1.1 var(--sans);margin:0;letter-spacing:-.3px}
.brand small{display:block;color:var(--label2);font-size:12px;margin-top:2px;font-weight:500}
.nav{display:flex;gap:2px;background:var(--fill);padding:2px;border-radius:11px}
.nav button{border:0;border-radius:9px;background:transparent;color:var(--label);padding:7px 15px;font:600 13px var(--sans);cursor:pointer;transition:background .18s ease,box-shadow .18s ease;letter-spacing:-.1px}
.nav button.active{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.13),0 1px 1px rgba(0,0,0,.04)}
.clockBox{text-align:right}
.clockBox .time{font:600 22px/1 var(--sans);font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.clockBox .date{font-size:11.5px;color:var(--label2);font-weight:500;margin-top:3px}
.title{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap;padding:6px 6px 0}
.title h2{font:700 32px/1.05 var(--sans);margin:0;letter-spacing:-1.1px;color:var(--label)}
.title p{margin:7px 0 0;color:var(--label2);font-size:13.5px;font-weight:500}
.title p b{color:var(--label);font-weight:600}
.pill{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:7px 13px;font-weight:600;font-size:12.5px}
.pill.live{background:rgba(52,199,89,.14);color:#1e8e3e;border:1px solid rgba(52,199,89,.22)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px rgba(52,199,89,.18)}
.gridMain{display:grid;grid-template-columns:340px 1fr 360px;gap:16px;align-items:start}
.stack{display:flex;flex-direction:column;gap:16px}
.card{padding:18px}
.cardTitle{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;gap:10px}
.cardTitle b{font:600 15px var(--sans);letter-spacing:-.2px}
.cardTitle span{font-size:12px;color:var(--label2);font-weight:500}
.metricBig{font:700 42px/1 var(--sans);letter-spacing:-1.6px;font-variant-numeric:tabular-nums}
.unit{font-size:15px;color:var(--label2);font-weight:600;margin-left:3px}
.badge{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;padding:6px 13px;color:#fff;font-weight:700;font-size:12px;letter-spacing:.2px;box-shadow:0 2px 9px rgba(0,0,0,.13)}
.miniGrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.mini{background:var(--fill);border-radius:14px;padding:13px 14px}
.mini small{display:block;color:var(--label2);font-weight:500;font-size:11.5px}
.mini b{display:block;font:600 23px var(--sans);margin-top:5px;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.hero{padding:20px;min-height:560px;position:relative;overflow:hidden}
.hero:before{content:"";position:absolute;width:430px;height:430px;border-radius:50%;background:radial-gradient(circle,rgba(0,122,255,.08),transparent 68%);top:40px;left:50%;transform:translateX(-50%);pointer-events:none}
.gaugeWrap{position:relative;display:grid;place-items:center;height:300px}
.gauge{width:240px;height:240px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--green) 0deg,var(--green) 180deg,rgba(118,118,128,.12) 180deg 360deg);box-shadow:0 12px 32px rgba(0,0,0,.08)}
.gaugeInner{width:172px;height:172px;border-radius:50%;background:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.07),inset 0 1px 0 #fff}
.gaugeInner .score{font:700 52px/1 var(--sans);letter-spacing:-2px;font-variant-numeric:tabular-nums}
.gaugeInner small{color:var(--label2);font-weight:500;font-size:12px;margin-top:2px}
.float{position:absolute;background:rgba(255,255,255,.78);border:1px solid rgba(255,255,255,.7);border-radius:16px;padding:10px 12px;box-shadow:0 6px 20px rgba(0,0,0,.09);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);display:flex;gap:9px;align-items:center}
.float .ico{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;color:#fff;font-weight:700;font-size:13px}
.float small{font-size:11px;color:var(--label2);font-weight:500;display:block}
.float b{font:600 16px var(--sans);font-variant-numeric:tabular-nums}
.f1{top:18px;left:0}.f2{top:42px;right:0}.f3{bottom:22px;left:18px}
.aiBox{margin-top:14px;border-radius:18px;background:var(--fill);padding:16px}
.aiBox b{font:600 15px var(--sans)}
.scan{height:36px;display:flex;align-items:end;gap:3px;overflow:hidden;margin-top:12px}
.bar{flex:1;min-width:3px;border-radius:4px 4px 0 0;background:linear-gradient(180deg,var(--teal),var(--blue));opacity:.5}
.action{padding:16px 18px;border-radius:18px;color:#fff;background:linear-gradient(135deg,var(--teal),var(--blue));box-shadow:0 10px 26px rgba(0,122,255,.22)}
.action small{display:block;opacity:.9;font-weight:500;margin-bottom:4px;font-size:12px}
.action b{font-size:17px;font-weight:600}
.oled{background:#000;border:1px solid rgba(60,60,67,.2);border-radius:16px;padding:16px;color:#7fe0ff;box-shadow:inset 0 0 26px rgba(40,150,220,.14),0 8px 22px rgba(0,0,0,.14)}
.oled .line{font:600 16px/1.7 var(--mono);letter-spacing:.3px;white-space:pre}
.oled .sep{height:1px;background:rgba(127,224,255,.2);margin:5px 0 8px}
.tabs>section{display:none}.tabs>section.active{display:block}
.aiGrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.aiMetric{padding:16px;border-radius:18px;background:var(--surface);border:1px solid rgba(255,255,255,.6);backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%);box-shadow:0 1px 2px rgba(0,0,0,.03),0 8px 22px rgba(0,0,0,.05)}
.aiMetric small{display:block;color:var(--label2);font-weight:500;font-size:11.5px}
.aiMetric b{display:block;font:600 25px var(--sans);margin-top:5px;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.chartCard{padding:18px}
.chartBox{height:260px}
.tableWrap{overflow:auto;border-radius:16px;border:1px solid var(--hair2)}
table{width:100%;border-collapse:collapse;background:rgba(255,255,255,.5)}
th,td{text-align:left;padding:9px 14px;font-size:13px;border-bottom:1px solid var(--hair2)}
th{color:var(--label2);font-size:11px;text-transform:uppercase;letter-spacing:.4px;font-weight:600}
td{font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.btn{border:0;background:var(--blue);color:#fff;border-radius:12px;padding:9px 15px;font:600 13px var(--sans);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;box-shadow:0 2px 9px rgba(0,122,255,.22)}
.btn:active{opacity:.85}
.footer{color:var(--label2);text-align:center;font-size:12px;padding:6px 0 14px;font-weight:500}
.devGrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.devCard{background:var(--surface);border:1px solid rgba(255,255,255,.6);border-radius:18px;padding:14px;display:flex;flex-direction:column;align-items:center;text-align:center;gap:7px;backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%)}
.devImg{width:100%;height:104px;border-radius:14px;background:linear-gradient(165deg,#fff,#eef1f6);display:grid;place-items:center;overflow:hidden;border:1px solid var(--hair2)}
.devImg svg{width:84%;height:84%}
.devCard small{color:var(--label2);font-weight:500;font-size:11px}
.devCard .nm{font:600 16px var(--sans)}
.devCard .st{font-size:11px;font-weight:600;color:#1e8e3e;display:inline-flex;align-items:center;gap:6px}
.devCard .st i{width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block;box-shadow:0 0 0 3px rgba(52,199,89,.18)}
button:focus-visible,a:focus-visible{outline:2px solid var(--blue);outline-offset:2px}

.overviewCharts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
.compareCard{padding:16px}.compareGrid{display:grid;grid-template-columns:1.05fr 1.35fr .85fr;gap:10px;align-items:stretch}.comparePanel{background:var(--fill);border-radius:16px;padding:12px;border:1px solid rgba(255,255,255,.35)}.comparePanel .head{font-weight:700;font-size:13px;margin-bottom:2px;display:flex;align-items:center;gap:8px}.comparePanel .sub{font-size:11px;color:var(--label2);font-weight:600;margin-bottom:10px}.compareRow{display:grid;grid-template-columns:1fr 1fr;gap:8px}.compareMini{background:rgba(255,255,255,.44);border-radius:13px;padding:9px 10px}.compareMini small{display:block;color:var(--label2);font-size:10.5px;font-weight:700}.compareMini b{display:block;margin-top:4px;font-size:18px;font-weight:800;letter-spacing:-.4px}.compareMini.co2{grid-column:1/3}.diffStack{display:grid;gap:8px}.diffBox{background:var(--fill);border-radius:15px;padding:12px;border:1px solid rgba(255,255,255,.35)}.diffBox small{display:block;color:var(--label2);font-size:10.5px;font-weight:700}.diffBox b{display:block;margin-top:5px;font-size:19px;font-weight:800}.insightLine{margin-top:9px;background:var(--fill);border-radius:14px;padding:9px 12px;color:var(--label);font-size:12.5px;font-weight:700;display:flex;gap:8px;align-items:center}.weatherIcon{width:26px;height:26px;border-radius:50%;display:inline-grid;place-items:center;background:linear-gradient(135deg,#eaf8ff,#dbeafe);border:1px solid rgba(0,122,255,.12)}

@media(prefers-reduced-motion:reduce){*{transition:none!important}}
@media(max-width:1180px){.gridMain{grid-template-columns:1fr}.overviewCharts{grid-template-columns:1fr}.compareGrid{grid-template-columns:1fr}.aiGrid{grid-template-columns:1fr 1fr}.devGrid{grid-template-columns:1fr 1fr}.hero{min-height:auto}.gaugeWrap{height:280px}}
@media(max-width:680px){.wrap{padding:14px}.compareRow{grid-template-columns:1fr}.compareMini.co2{grid-column:auto}.title h2{font-size:27px}.nav{width:100%;overflow:auto}.aiGrid,.miniGrid,.devGrid{grid-template-columns:1fr}.clockBox{text-align:left}.float{position:static;margin-top:10px}.gaugeWrap{height:auto;display:block}.gauge{margin:18px auto}}
</style>
</head>
<body>
<div class="wrap">
  <header class="glass topbar"><div class="brand"><div class="logo">≋</div><div><h1 class="sg">AirSense AIoT</h1><small>Realtime Air Quality Station</small></div></div><nav class="nav"><button class="active" data-tab="overview">Ringkasan</button><button data-tab="ai">Analisis AI</button><button data-tab="devices">OLED & Data</button></nav><div class="clockBox"><div class="time" id="clock">--:--:--</div><div class="date" id="date">WIB</div></div></header>
  <div class="title"><div><h2>Dashboard Kualitas Udara</h2><p><b>__DEVICE__</b> · __LOCATION__ · Mode outdoor: DHT22 realtime dibandingkan BMKG</p></div><div class="pill live"><span class="dot" id="liveDot"></span><span id="liveLabel">Menghubungkan…</span></div></div>
  <main class="tabs">
    <section id="overview" class="active"><div class="gridMain"><div class="stack"><div class="glass card"><div class="cardTitle"><b>CO2 Realtime</b><span id="lastUpdate">--</span></div><div style="display:flex;align-items:end;justify-content:space-between;gap:10px"><div><span class="metricBig" id="co2Now">--</span><span class="unit">ppm</span></div><span class="badge" id="catBadge">--</span></div><div class="chartBox" style="height:150px;margin-top:10px"><canvas id="co2Mini"></canvas></div></div><div class="glass card"><div class="cardTitle"><b>Suhu & RH Outdoor</b><span>DHT22 realtime alat</span></div><div class="miniGrid"><div class="mini"><small>Suhu Alat</small><b><span id="tempNow">--</span>°C</b></div><div class="mini"><small>RH Alat</small><b><span id="humNow">--</span>%</b></div><div class="mini"><small>Kondisi Termal</small><b id="comfortNow" style="font-size:20px">--</b></div><div class="mini"><small>Raw MQ135</small><b id="rawNow">--</b></div></div></div><div class="action" id="actionBox"><small>Rekomendasi AI</small><b id="actionText">Menunggu data sensor…</b></div></div><div class="glass hero"><div class="cardTitle"><b>Indeks Kualitas Udara</b><span id="aiSource">AI Hybrid</span></div><div class="gaugeWrap"><div class="gauge" id="gauge"><div class="gaugeInner"><div class="score" id="scoreNow">--</div><small>Air Score</small><span class="badge" id="heroCat" style="margin-top:10px">--</span></div></div><div class="float f1"><div class="ico" style="background:#FF9500">T</div><div><small>Suhu</small><b><span id="floatTemp">--</span>°C</b></div></div><div class="float f2"><div class="ico" style="background:#007AFF">RH</div><div><small>Kelembapan</small><b><span id="floatHum">--</span>%</b></div></div><div class="float f3"><div class="ico" style="background:#30B0C7">V</div><div><small>Ventilasi</small><b id="ventText">--</b></div></div></div><div class="aiBox"><div style="display:flex;align-items:center;justify-content:space-between;gap:12px"><div><b>Proyeksi 10 menit</b><div style="color:var(--muted);font-size:12px;font-weight:700">Prediksi tren CO2 jangka pendek</div></div><div class="sg" style="font-size:25px;font-weight:800"><span id="proj10">--</span> ppm</div></div><div class="scan" id="scanBars"></div></div></div><div class="stack"><div class="glass card"><div class="cardTitle"><b>OLED Hardware</b><span>mirror</span></div><div class="oled"><div class="line" id="oled1">AIoT      --:--:--</div><div class="sep"></div><div class="line" id="oled2">Suhu : --.- C</div><div class="line" id="oled3">RH   : --.- %</div><div class="line" id="oled4">Gas  : ----</div><div class="line" id="oled5">CO2  : ---- ppm</div></div></div><div class="glass card"><div class="cardTitle"><b>Ringkasan AI</b><span>120 data terakhir</span></div><div class="miniGrid"><div class="mini"><small>Risiko</small><b id="riskNow">--</b></div><div class="mini"><small>Tren</small><b id="trendNow">--</b></div><div class="mini"><small>Laju</small><b><span id="rateNow">--</span>/m</b></div><div class="mini"><small>ETA 1000 ppm</small><b id="etaNow">--</b></div></div></div><div class="glass card compareCard"><div class="cardTitle"><b>Perbandingan Sensor Outdoor vs BMKG</b><span>ADM4 32.04.08.2001</span></div><div class="compareGrid"><div class="comparePanel"><div class="head"><span class="weatherIcon">🌦</span>BMKG</div><div class="sub">Prakiraan Lengkong</div><div class="compareRow"><div class="compareMini"><small>Suhu BMKG</small><b><span id="bmkgTemp">--</span>°C</b></div><div class="compareMini"><small>RH BMKG</small><b><span id="bmkgHum">--</span>%</b></div></div></div><div class="comparePanel"><div class="head"><span class="weatherIcon">📡</span>Sensor Outdoor</div><div class="sub">Realtime alat</div><div class="compareRow"><div class="compareMini"><small>Suhu Sensor</small><b><span id="sensorTempMini">--</span>°C</b></div><div class="compareMini"><small>RH Sensor</small><b><span id="sensorHumMini">--</span>%</b></div><div class="compareMini co2"><small>CO2 Sensor</small><b><span id="sensorCo2Mini">--</span> ppm</b></div></div></div><div class="diffStack"><div class="diffBox"><small>Selisih Suhu</small><b id="diffTemp">--</b></div><div class="diffBox"><small>Selisih RH</small><b id="diffHum">--</b></div></div></div><div class="insightLine"><span>💡</span><span id="bmkgInsight">Menunggu data BMKG dan sensor outdoor.</span></div></div></div></div><div class="overviewCharts"><div class="glass chartCard"><div class="cardTitle"><b>Grafik CO2 + Moving Average + Forecast</b><span>Realtime</span></div><div class="chartBox"><canvas id="co2Chart"></canvas></div></div><div class="glass chartCard"><div class="cardTitle"><b>Grafik Suhu dan Kelembapan</b><span>DHT22</span></div><div class="chartBox"><canvas id="thChart"></canvas></div></div></div></section>
    <section id="ai"><div class="stack"><div class="aiGrid"><div class="aiMetric"><small>Rata-rata CO2</small><b><span id="avgCo2">--</span> ppm</b></div><div class="aiMetric"><small>Puncak CO2</small><b><span id="maxCo2">--</span> ppm</b></div><div class="aiMetric"><small>Standar Deviasi</small><b><span id="stdCo2">--</span></b></div><div class="aiMetric"><small>Confidence AI</small><b><span id="r2Now">--</span></b></div><div class="aiMetric"><small>Akumulasi</small><b id="accumNow">--</b></div><div class="aiMetric"><small>Anomali</small><b id="anomNow">--</b></div><div class="aiMetric"><small>Outdoor: DHT22 vs BMKG</small><b id="autoNow">--</b></div><div class="aiMetric"><small>Sumber Prediksi</small><b id="predSrc">Hybrid Air Quality Risk</b></div></div><div class="glass chartCard"><div class="cardTitle"><b>Interpretasi AI</b><span>Hybrid Air Quality Risk</span></div><div class="miniGrid"><div class="mini"><small>CO2 5 menit</small><b><span id="ai5m">--</span> ppm</b></div><div class="mini"><small>CO2 10 menit</small><b><span id="ai10m">--</span> ppm</b></div><div class="mini"><small>Sumber Kondisi</small><b id="aiCause" style="font-size:19px">--</b></div><div class="mini"><small>Aksi</small><b id="aiActionMini" style="font-size:18px">--</b></div></div></div></div></section>
    <section id="devices"><div class="stack"><div class="glass card"><div class="cardTitle"><b>Perangkat Online</b><span>UNO Q · DHT22 · MQ135 · OLED</span></div><div class="devGrid"><div class="devCard"><div class="devImg">__SVG_UNOQ__</div><small>Board (MPU + MCU)</small><div class="nm">Arduino UNO Q</div><span class="st"><i></i>ONLINE</span></div><div class="devCard"><div class="devImg">__SVG_DHT22__</div><small>Sensor Suhu / RH</small><div class="nm">DHT22</div><span class="st"><i></i>ONLINE</span></div><div class="devCard"><div class="devImg">__SVG_MQ135__</div><small>Sensor Gas / CO2</small><div class="nm">MQ135</div><span class="st"><i></i>ONLINE</span></div><div class="devCard"><div class="devImg">__SVG_OLED__</div><small>Display Lokal</small><div class="nm">OLED SSD1306</div><span class="st"><i></i>ONLINE</span></div></div></div><div class="glass card"><div class="cardTitle"><b>Data Terbaru</b><span>6 data unik terakhir</span><a class="btn" href="/api/export/__DEVICE__">Unduh CSV</a></div><div class="tableWrap"><table><thead><tr><th>Jam</th><th>Suhu</th><th>RH</th><th>CO2</th><th>Kategori</th><th>Tren</th></tr></thead><tbody id="recentRows"><tr><td colspan="6">Menunggu data…</td></tr></tbody></table></div></div></div></section>
  </main>
  <div class="footer">AirSense AIoT · __DEVICE__ · Febryan Ferdi Rafi · PythonAnywhere Flask Dashboard</div>
</div>
<script>
const DEVICE="__DEVICE__";let co2Chart=null,thChart=null,co2Mini=null;const $=id=>document.getElementById(id);const fmt=(v,d=1)=>v===null||v===undefined||Number.isNaN(Number(v))?"--":Number(v).toFixed(d).replace(/\.0$/,"" );function setText(id,v){const e=$(id);if(e)e.textContent=v}function setBadge(id,text,color){const e=$(id);if(!e)return;e.textContent=text||"--";e.style.background=color||"#8E8E93"}function prettyPredSource(src){const s=String(src||"").toLowerCase();if(s.includes("hybrid_air_quality_risk"))return "Hybrid Air Quality Risk";if(s.includes("linear"))return "Hybrid Air Quality Risk";if(s.includes("lstm"))return "Forecast Model";return src||"--"}function prettyActivityText(latest){if(!latest)return "--";const label=String(latest.occupancy_label||"").toUpperCase();const pct=Number(latest.occupancy_percent??0);const co2=Number(latest.co2??0);const hum=Number(latest.kelembapan??latest.humidity??0);const rate=Number(latest.rate??latest.rate_ppm_per_min??0);if(label==="TERISI"&&pct>=50)return "Ada orang · "+fmt(pct,0)+"%";if(co2>=520||hum>=65||rate>1.5)return "Ada aktivitas";if(label==="KOSONG")return "Tidak terdeteksi";return "Rendah"}function setBmkgCard(j,latest){const b=j.bmkg||{};const c=b.current||{};const dhtT=Number(latest&&latest.suhu);const dhtH=Number(latest&&latest.kelembapan);const co2=Number(latest&&latest.co2);setText("sensorTempMini",fmt(dhtT,1));setText("sensorHumMini",fmt(dhtH,1));setText("sensorCo2Mini",fmt(co2,0));if(b.ok&&c){const bt=Number(c.temp);const bh=Number(c.humidity);setText("bmkgTemp",fmt(bt,1));setText("bmkgHum",fmt(bh,0));let insight="Referensi BMKG tersedia untuk pembanding kondisi outdoor.";if(!Number.isNaN(dhtT)&&!Number.isNaN(bt)){const dt=dhtT-bt;setText("diffTemp",(dt>=0?"+":"")+fmt(dt,1)+"°C");}else{setText("diffTemp","--");}if(!Number.isNaN(dhtH)&&!Number.isNaN(bh)){const dh=dhtH-bh;setText("diffHum",(dh>=0?"+":"")+fmt(dh,0)+"% RH");}else{setText("diffHum","--");}if(!Number.isNaN(dhtT)&&!Number.isNaN(bt)&&!Number.isNaN(dhtH)&&!Number.isNaN(bh)){const dt=dhtT-bt,dh=dhtH-bh;let panas=Math.abs(dt)>=2?(dt>0?"lebih panas":"lebih dingin"):"mirip suhu BMKG";let lembap=Math.abs(dh)>=5?(dh>0?"lebih lembap":"lebih kering"):"mirip RH BMKG";insight="Sensor lokal terukur "+panas+" dan "+lembap+" dibanding referensi BMKG.";setText("autoNow","DHT "+fmt(dhtT,1)+"°C vs BMKG "+fmt(bt,1)+"°C");}else{setText("autoNow",fmt(bt,1)+"°C · "+fmt(bh,0)+"% RH");}setText("bmkgInsight",insight);}else{setText("bmkgTemp","--");setText("bmkgHum","--");setText("diffTemp","--");setText("diffHum","--");setText("bmkgInsight","BMKG belum tersedia, cek koneksi atau kode ADM4.");setText("autoNow","BMKG belum tersedia");}}function wibNow(){return new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Jakarta",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date())}function wibDate(){return new Intl.DateTimeFormat("id-ID",{timeZone:"Asia/Jakarta",weekday:"long",day:"2-digit",month:"long",year:"numeric"}).format(new Date())}function tickClock(){var t=wibNow();setText("clock",t);setText("date",wibDate()+" · WIB");var o=$("oled1");if(o)o.textContent="AIoT      "+t}setInterval(tickClock,1000);tickClock();document.querySelectorAll(".nav button").forEach(btn=>{btn.addEventListener("click",()=>{document.querySelectorAll(".nav button").forEach(b=>b.classList.remove("active"));document.querySelectorAll(".tabs>section").forEach(s=>s.classList.remove("active"));btn.classList.add("active");$(btn.dataset.tab).classList.add("active");setTimeout(()=>{if(co2Chart)co2Chart.resize();if(thChart)thChart.resize();if(co2Mini)co2Mini.resize()},50)})});function makeBars(){const box=$("scanBars");if(!box)return;box.innerHTML="";for(let i=0;i<44;i++){const b=document.createElement("div");b.className="bar";b.style.height=(20+Math.abs(Math.sin(i/4))*70)+"%";box.appendChild(b)}}makeBars();function chartOptions(){return{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{boxWidth:10,usePointStyle:true}},tooltip:{mode:"index",intersect:false}},scales:{x:{grid:{display:false},ticks:{maxTicksLimit:5}},y:{grid:{color:"rgba(15,39,66,.08)"},ticks:{maxTicksLimit:5}}},interaction:{mode:"index",intersect:false},elements:{point:{radius:0},line:{tension:.35,borderWidth:2.5}}}}function upsertChart(current,canvas,cfg){if(!canvas)return null;if(current){current.data=cfg.data;current.options=cfg.options;current.update("none");return current}return new Chart(canvas,cfg)}async function load(){try{const r=await fetch("/api/insights/"+DEVICE+"?limit=2500",{cache:"no-store"});const j=await r.json();if(!j.ok||!j.latest)throw new Error("no data");render(j);setText("liveLabel","Live · data masuk");$("liveDot").style.background="#34C759"}catch(e){setText("liveLabel","Menunggu data");$("liveDot").style.background="#FF9500"}}function render(j){const latest=j.latest;if(latest&&latest.payload_json){try{Object.assign(latest,JSON.parse(latest.payload_json))}catch(e){}}const ai=j.ai||{},chart=j.chart||{};const kl=ai.klasifikasi||{},pred=ai.prediksi||{},risk=ai.risiko||{},vent=ai.ventilasi||{},anal=ai.analisis||{};const action=ai.keputusan||{},proj=ai.proyeksi10||{},accum=ai.akumulasi||{},anom=ai.anomali||{},otom=ai.otomasi||{};setText("co2Now",fmt(latest.co2,0));setText("tempNow",fmt(latest.suhu,1));setText("humNow",fmt(latest.kelembapan,1));setText("floatTemp",fmt(latest.suhu,1));setText("floatHum",fmt(latest.kelembapan,1));setText("rawNow",latest.gas_raw??"--");setText("comfortNow",ai.kenyamanan||latest.comfort||"--");setText("lastUpdate",(latest.created_at||"").slice(11,19)||"--");setBadge("catBadge",kl.kategori||latest.kategori,kl.warna);setBadge("heroCat",kl.kategori||latest.kategori,kl.warna);setText("scoreNow",ai.skor??"--");const deg=Math.max(0,Math.min(360,(Number(ai.skor||0)/100)*360));$("gauge").style.background=`conic-gradient(${kl.warna||"#34C759"} 0deg, ${kl.warna||"#34C759"} ${deg}deg, rgba(15,39,66,.08) ${deg}deg 360deg)`;setText("ventText",(vent.skor??"--")+"% · "+(vent.status||"--"));setText("proj10",fmt(latest.ai_forecast_10m??proj.ppm,0));setText("aiSource",latest.edge_ai_mode?"EDGE AI":prettyPredSource(proj.src));setText("actionText",latest.ai_action||action.aksi||"--");$("actionBox").style.background=`linear-gradient(135deg,${action.warna||"#30B0C7"},#007AFF)`;setText("riskNow",latest.ai_risk_score!==undefined?(latest.ai_risk_score+" · "+(latest.ai_risk_level||"--")):((risk.skor??"--")+" · "+(risk.kategori||"--")));setText("trendNow",pred.tren||"--");setText("rateNow",fmt(pred.slope,2));setText("etaNow",(latest.ai_eta_1000_min??pred.eta_menit)===null||(latest.ai_eta_1000_min??pred.eta_menit)===undefined?"--":(latest.ai_eta_1000_min??pred.eta_menit)+" mnt");setText("avgCo2",fmt(anal.avg,0));setText("maxCo2",fmt(anal.max,0));setText("stdCo2",fmt(anal.std,1));setText("r2Now",latest.ai_confidence!==undefined?(fmt(latest.ai_confidence,0)+"%"):(fmt(pred.r2,2)));setText("accumNow",latest.ai_cause||accum.label||"--");setText("anomNow",anom.now?("YA · z="+fmt(anom.z,2)):"Tidak");setBmkgCard(j,latest);setText("predSrc",prettyPredSource(latest.edge_ai_mode||proj.src));setText("ai5m",fmt(latest.ai_forecast_5m,0));setText("ai10m",fmt(latest.ai_forecast_10m,0));setText("aiCause",latest.ai_cause||accum.label||"--");setText("aiActionMini",latest.ai_action||action.aksi||"--");if(j.bmkg&&j.bmkg.ok&&j.bmkg.current){var ab=$("actionBox");if(ab)ab.title="Referensi BMKG: "+(j.bmkg.current.weather||"--")+" · "+fmt(j.bmkg.current.temp,1)+"°C · RH "+fmt(j.bmkg.current.humidity,0)+"%";}if(j.oled){setText("oled2",j.oled[1]);setText("oled3",j.oled[2]);setText("oled4",j.oled[3]);setText("oled5",j.oled[4])}const labels=chart.labels||[],co2=chart.co2||[],ma=chart.ma||[],suhu=chart.suhu||[],hum=chart.hum||[];const fLabels=chart.forecast_labels||[],forecast=chart.forecast||[];co2Mini=upsertChart(co2Mini,$("co2Mini"),{type:"line",data:{labels:labels,datasets:[{label:"CO2",data:co2,borderColor:kl.warna||"#34C759",backgroundColor:"rgba(52,199,89,.12)",fill:true}]},options:{...chartOptions(),plugins:{legend:{display:false}},scales:{x:{display:false},y:{display:false}}}});co2Chart=upsertChart(co2Chart,$("co2Chart"),{type:"line",data:{labels:[...labels,...fLabels],datasets:[{label:"CO2",data:[...co2,...Array(forecast.length).fill(null)],borderColor:kl.warna||"#34C759",backgroundColor:"rgba(52,199,89,.08)",fill:true},{label:"MA",data:[...ma,...Array(forecast.length).fill(null)],borderColor:"#007AFF"},{label:"Forecast",data:[...Array(co2.length).fill(null),...forecast],borderColor:"#FF9500",borderDash:[7,5]}]},options:chartOptions()});thChart=upsertChart(thChart,$("thChart"),{type:"line",data:{labels:labels,datasets:[{label:"Suhu °C",data:suhu,borderColor:"#FF9500",yAxisID:"y"},{label:"RH %",data:hum,borderColor:"#007AFF",yAxisID:"y1"}]},options:{...chartOptions(),scales:{x:{grid:{display:false},ticks:{maxTicksLimit:5}},y:{position:"left",grid:{color:"rgba(15,39,66,.08)"}},y1:{position:"right",grid:{drawOnChartArea:false}}}}});const rows=(j.recent||[]).map(r=>`<tr><td>${(r.created_at||"").slice(11,19)}</td><td>${fmt(r.suhu,1)}°C</td><td>${fmt(r.kelembapan,1)}%</td><td>${fmt(r.co2,0)} ppm</td><td>${r.kategori||"--"}</td><td>${r.trend||pred.tren||"--"}</td></tr>`).join("");$("recentRows").innerHTML=rows||'<tr><td colspan="6">Belum ada data.</td></tr>'}load();setInterval(load,2000);
</script>
</body>
</html>'''


@app.route("/", methods=["GET"])
def dashboard():
    html = DASHBOARD_HTML
    replacements = {
        "__DEVICE__": DEVICE,
        "__LOCATION__": DEVICE_LOCATION,
        "__LAT__": str(DEVICE_LAT),
        "__LON__": str(DEVICE_LON),
        "__SVG_UNOQ__": SVG_UNOQ,
        "__SVG_DHT22__": SVG_DHT22,
        "__SVG_MQ135__": SVG_MQ135,
        "__SVG_OLED__": SVG_OLED,
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return Response(html, mimetype="text/html")


# PythonAnywhere akan menjalankan variable app.
# Blok ini hanya untuk test lokal.
if __name__ == "__main__":
    app.run(debug=True)