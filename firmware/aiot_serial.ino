/*
  AI IoT Air Quality Monitor - MCU firmware for Arduino UNO Q

  New architecture:
  - No RouterBridge.
  - No App Lab runtime dependency.
  - MCU reads sensors, updates OLED, and sends JSON Lines to Linux over serial.

  Pins:
  - DHT22 data pin  : D8
  - MQ135 analog pin: A0, through voltage divider R1=10k and R2=20k
  - OLED SSD1306    : hardware I2C, default SDA/SCL pins for UNO Q

  Serial:
  - Uses Serial at 115200 baud by default.
  - If your board wiring exposes MCU<->Linux on another UART, change
    SENSOR_SERIAL below to Serial1 or the correct port.
*/

#include <Arduino.h>
#include <DHT.h>
#include <U8g2lib.h>
#include <math.h>
#include <stdio.h>

#define SENSOR_SERIAL Serial

#define DHTPIN   8
#define DHTTYPE  DHT22
#define MQ135PIN A0

static const unsigned long SERIAL_BAUD = 115200;
static const unsigned long MQ_INTERVAL_MS = 2000;
static const unsigned long DHT_INTERVAL_MS = 6000;
static const unsigned long SEND_INTERVAL_MS = 2000;
static const unsigned long OLED_INTERVAL_MS = 1000;
static const int MQ_OVERSAMPLE = 64;

// MQ135 constants mirrored from the Python side. RO_DISPLAY is only for the
// simple local OLED estimate; Python remains the source of truth for server data.
static const float VCC_SENSOR = 5.0f;
static const float VREF = 3.3f;
static const float ADC_MAX = 4095.0f;
static const float RL = 10.0f;
static const float R1_DIV = 10.0f;
static const float R2_DIV = 20.0f;
static const float DIVIDER_RATIO = R2_DIV / (R1_DIV + R2_DIV);
static const float PARA_A = 116.6020682f;
static const float PARA_B = -2.769034857f;
static const float CORA = 0.00035f;
static const float CORB = 0.02718f;
static const float CORC = 1.39538f;
static const float CORD = 0.0018f;
static const float RO_DISPLAY = 1993.7677f;

DHT dht(DHTPIN, DHTTYPE);
U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

unsigned long lastMqMs = 0;
unsigned long lastDhtMs = 0;
unsigned long lastSendMs = 0;
unsigned long lastOledMs = 0;
unsigned long seq = 0;

int gasRaw = 0;
float lastTemp = 0.0f;
float lastHum = 0.0f;
bool hasValidDht = false;
bool lastDhtOk = false;
unsigned long lastValidDhtMs = 0;

float correctionFactor(float temp, float hum) {
  return CORA * temp * temp - CORB * temp + CORC - (hum - 33.0f) * CORD;
}

float mq135Resistance(int raw, float temp, float hum) {
  if (raw <= 0) return NAN;

  float vAdc = (raw / ADC_MAX) * VREF;
  float vOut = vAdc / DIVIDER_RATIO;
  if (vOut <= 0.0f || vOut >= VCC_SENSOR) return NAN;

  float rs = RL * (VCC_SENSOR - vOut) / vOut;
  float cf = correctionFactor(temp, hum);
  if (cf > 0.0f) rs = rs / cf;
  return rs;
}

float estimatePpm(int raw) {
  float temp = hasValidDht ? lastTemp : 20.0f;
  float hum = hasValidDht ? lastHum : 33.0f;
  float rs = mq135Resistance(raw, temp, hum);
  if (isnan(rs) || rs <= 0.0f || RO_DISPLAY <= 0.0f) return NAN;

  float ratio = rs / RO_DISPLAY;
  if (ratio <= 0.0f) return NAN;

  float ppm = PARA_A * pow(ratio, PARA_B);
  if (isnan(ppm) || isinf(ppm)) return NAN;
  if (ppm < 0.0f) ppm = 0.0f;
  if (ppm > 5000.0f) ppm = 5000.0f;
  return ppm;
}

void readMq135() {
  long acc = 0;
  for (int i = 0; i < MQ_OVERSAMPLE; i++) {
    acc += analogRead(MQ135PIN);
  }
  gasRaw = (int)(acc / MQ_OVERSAMPLE);
}

void readDht22() {
  float t = dht.readTemperature();
  float h = dht.readHumidity();

  bool ok = !isnan(t) && !isnan(h) && t >= -20.0f && t <= 80.0f && h >= 0.0f && h <= 100.0f;
  if (ok) {
    lastTemp = t;
    lastHum = h;
    hasValidDht = true;
    lastValidDhtMs = millis();
    lastDhtOk = true;
  } else {
    lastDhtOk = false;
  }
}

void sendJsonLine() {
  if (gasRaw <= 0) return;

  seq++;
  SENSOR_SERIAL.print('{');
  if (hasValidDht) {
    SENSOR_SERIAL.print("\"temp\":");
    SENSOR_SERIAL.print(lastTemp, 1);
    SENSOR_SERIAL.print(",\"hum\":");
    SENSOR_SERIAL.print(lastHum, 1);
    SENSOR_SERIAL.print(',');
  }
  SENSOR_SERIAL.print("\"gas_raw\":");
  SENSOR_SERIAL.print(gasRaw);
  SENSOR_SERIAL.print(",\"seq\":");
  SENSOR_SERIAL.print(seq);
  SENSOR_SERIAL.print(",\"millis\":");
  SENSOR_SERIAL.print(millis());
  SENSOR_SERIAL.print(",\"dht_ok\":");
  SENSOR_SERIAL.print(lastDhtOk ? "true" : "false");
  SENSOR_SERIAL.println('}');
}

void drawOled() {
  char uptime[14];
  unsigned long seconds = millis() / 1000UL;
  snprintf(uptime, sizeof(uptime), "%02lu:%02lu:%02lu",
           seconds / 3600UL, (seconds / 60UL) % 60UL, seconds % 60UL);

  oled.clearBuffer();
  oled.setFont(u8g2_font_6x12_tf);
  oled.setCursor(0, 10);
  oled.print("AIoT Serial");
  int uptimeWidth = oled.getStrWidth(uptime);
  oled.setCursor(128 - uptimeWidth, 10);
  oled.print(uptime);
  oled.drawHLine(0, 13, 128);

  oled.setCursor(0, 26);
  oled.print("Suhu : ");
  if (hasValidDht) {
    oled.print(lastTemp, 1);
    oled.print(" C");
  } else {
    oled.print("--.- C");
  }

  oled.setCursor(0, 38);
  oled.print("RH   : ");
  if (hasValidDht) {
    oled.print(lastHum, 1);
    oled.print(" %");
  } else {
    oled.print("--.- %");
  }

  oled.setCursor(0, 50);
  oled.print("Gas  : ");
  oled.print(gasRaw);

  oled.setCursor(0, 62);
  float ppm = estimatePpm(gasRaw);
  if (!isnan(ppm)) {
    oled.print("CO2~ ");
    oled.print((int)round(ppm));
    oled.print("ppm ");
  } else {
    oled.print("CO2~ ----ppm ");
  }
  oled.print(lastDhtOk ? "OK" : (hasValidDht ? "DHT*" : "WAIT"));
  oled.sendBuffer();
}

void setup() {
  SENSOR_SERIAL.begin(SERIAL_BAUD);
  analogReadResolution(12);
  dht.begin();
  oled.begin();

  readMq135();
  drawOled();
}

void loop() {
  unsigned long now = millis();

  if (now - lastMqMs >= MQ_INTERVAL_MS) {
    lastMqMs = now;
    readMq135();
  }

  if (now - lastDhtMs >= DHT_INTERVAL_MS) {
    lastDhtMs = now;
    readDht22();
  }

  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    lastSendMs = now;
    sendJsonLine();
  }

  if (now - lastOledMs >= OLED_INTERVAL_MS) {
    lastOledMs = now;
    drawOled();
  }
}
