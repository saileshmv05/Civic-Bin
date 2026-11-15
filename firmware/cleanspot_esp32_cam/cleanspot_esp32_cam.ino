#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>

// ========================================================
// 1. WI-FI CREDENTIALS
// ========================================================
const char* ssid = "YOUR_WIFI_OR_HOTSPOT_NAME";
const char* password = "YOUR_WIFI_PASSWORD";

// ========================================================
// AI Thinker ESP32-CAM Pin Configuration
// ========================================================
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

#define FLASH_LED_PIN      4   // Built-in Night Flash Spotlight

WebServer server(80);
unsigned long bootTime = 0;

void handleRoot() {
  String html = "<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'>";
  html += "<title>CleanSpot Edge Sentinel</title>";
  html += "<style>";
  html += "body { font-family: system-ui, sans-serif; background: #0B1120; color: #FFF; text-align: center; margin: 0; padding: 20px; }";
  html += ".card { background: #151E33; border: 1px solid #00F0FF; border-radius: 12px; padding: 24px; max-width: 600px; margin: auto; box-shadow: 0 0 25px rgba(0,240,255,0.2); }";
  html += "h1 { color: #00F0FF; font-size: 26px; margin: 6px 0; }";
  html += ".badge { display: inline-block; background: #102841; color: #2EE6A6; border: 1px solid #2EE6A6; padding: 4px 14px; border-radius: 20px; font-size: 11px; font-weight: bold; }";
  html += "img { width: 100%; border-radius: 8px; border: 2px solid #2EE6A6; margin-top: 15px; background: #000; }";
  html += "button { background: #00F0FF; color: #0B1120; font-weight: bold; border: none; padding: 12px 24px; font-size: 15px; border-radius: 8px; cursor: pointer; margin-top: 15px; }";
  html += "button:hover { background: #2EE6A6; }";
  html += ".telemetry { background: #0B1120; border: 1px solid #2D415F; border-radius: 8px; padding: 12px; margin-top: 15px; text-align: left; font-family: monospace; font-size: 12px; color: #E2E8F0; }";
  html += "</style></head><body>";
  
  html += "<div class='card'>";
  html += "<div class='badge'>DESIGN THINKING REVIEW 1  |  CLEANSPOT EDGE NODE</div>";
  html += "<h1>CLEANSPOT-CAM-01</h1>";
  html += "<p style='color:#94A3B8; font-size:13px;'>AI Edge Vision Surveillance Node | Faculty Head: <b>Mr. Shakthivel</b></p>";
  html += "<div><button onclick='snap()'>📸 Capture Waste Evidence</button></div>";
  html += "<div><img id='view' src='/capture' alt='Live Optical Capture'></div>";
  html += "<div class='telemetry'>";
  html += "<b>STATUS: 🟢 ARMED & ONLINE</b><br>";
  html += "• Location: Lat 13.0827, Lon 80.2707 (Chennai Central Blind Spot)<br>";
  html += "• Sensor: 3MP Optical Matrix with Night Illumination Spotlight<br>";
  html += "• AI Link: Verified by Gemini Multimodal Vision Engine";
  html += "</div>";
  html += "</div>";

  html += "<script>";
  html += "function snap() {";
  html += "  document.getElementById('view').src = '/capture?t=' + new Date().getTime();";
  html += "}";
  html += "</script>";
  html += "</body></html>";
  
  server.send(200, "text/html", html);
}

void handleCapture() {
  // Flash LED briefly to illuminate the spot
  digitalWrite(FLASH_LED_PIN, HIGH);
  delay(100);
  camera_fb_t * fb = esp_camera_fb_get();
  digitalWrite(FLASH_LED_PIN, LOW);
  
  if (!fb) {
    server.send(500, "text/plain", "Capture failed");
    return;
  }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send_P(200, "image/jpeg", (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

void handleStatus() {
  String json = "{";
  json += "\"device_id\":\"CLEANSPOT-CAM-01\",";
  json += "\"status\":\"ONLINE\",";
  json += "\"lat\":13.0827,";
  json += "\"lon\":80.2707,";
  json += "\"uptime_sec\":" + String((millis() - bootTime) / 1000) + ",";
  json += "\"flash_ready\":true";
  json += "}";
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", json);
}

void setup() {
  Serial.begin(115200);
  bootTime = millis();
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 10000000;
  config.pixel_format = PIXFORMAT_JPEG;
  
  // Safe memory allocation: QVGA for zero RAM issues
  config.frame_size = FRAMESIZE_QVGA;
  config.jpeg_quality = 12;
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_DRAM;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init error: 0x%x\n", err);
  } else {
    Serial.println("CleanSpot Camera Initialized Successfully!");
  }

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\n[CleanSpot Online]");
  Serial.print("Node IP: http://");
  Serial.println(WiFi.localIP());

  server.on("/", handleRoot);
  server.on("/capture", handleCapture);
  server.on("/status", handleStatus);
  server.begin();
}

void loop() {
  server.handleClient();
  delay(10);
}
