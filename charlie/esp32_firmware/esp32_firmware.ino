/**
 * Charlie ESP32 全屋语音节点
 * 
 * 遗留参考代码：实际部署用预编译 bin 固件（xiaozhi v2.1.0 WebSocket 协议，经 /esp32-setup 页面烧录），
 * 本 .ino 为历史 HTTP POST 版本。
 * 
 * 硬件: ESP32 + INMP441(I2S麦克风) + MAX98357(I2S功放+喇叭)
 * 接线:
 *   INMP441:  VDD→3.3V  GND→GND  SD→GPIO32  WS→GPIO25  SCK→GPIO33  L/R→GND
 *   MAX98357: VIN→5V    GND→GND  DIN→GPIO26  BCLK→GPIO27  LRC→GPIO14
 *   LED:      GPIO2 (内置LED)
 *   Button:   GPIO0 (BOOT按钮，按下开始录音)
 * 
 * 工作流程:
 *   1. 按住按钮 → LED亮 → 开始录音
 *   2. 松开按钮 → LED灭 → 发送音频到Charlie服务端
 *   3. 接收回复音频 → 通过喇叭播放
 *   4. 播放完毕 → 等待下次按钮
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <ArduinoJson.h>

// ===== WiFi配置 =====
const char* WIFI_SSID = "你的WiFi名称";
const char* WIFI_PASSWORD = "你的WiFi密码";
const char* SERVER_URL = "http://192.168.1.12:8000/api/voice/stream";

// ===== I2S引脚配置 =====
#define I2S_MIC_WS   25   // INMP441 L/R
#define I2S_MIC_SCK  33   // INMP441 SCK
#define I2S_MIC_SD   32   // INMP441 SD

#define I2S_SPK_BCLK 27   // MAX98357 BCLK
#define I2S_SPK_LRC  14   // MAX98357 LRC
#define I2S_SPK_DIN  26   // MAX98357 DIN

#define BUTTON_PIN   0    // BOOT按钮
#define LED_PIN      2    // 内置LED

// ===== 音频配置 =====
#define SAMPLE_RATE  16000
#define BUFFER_SIZE  512
#define RECORD_SECONDS_MAX 10  // 最长录音10秒

// ===== 全局变量 =====
bool isRecording = false;
uint8_t* audioBuffer = nullptr;
size_t audioBufferSize = 0;
size_t audioBufferPos = 0;

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  
  // 连接WiFi
  Serial.println("\n🔌 连接WiFi...");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    Serial.print(".");
    attempts++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(LED_PIN, LOW);
    Serial.println("\n✅ WiFi已连接");
    Serial.print("📡 IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n❌ WiFi连接失败，5秒后重启");
    delay(5000);
    ESP.restart();
  }
  
  // 初始化I2S麦克风
  i2s_config_t i2s_mic_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 64,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };
  
  i2s_pin_config_t mic_pins = {
    .bck_io_num = I2S_MIC_SCK,
    .ws_io_num = I2S_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_MIC_SD
  };
  
  i2s_driver_install(I2S_NUM_0, &i2s_mic_config, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &mic_pins);
  i2s_start(I2S_NUM_0);
  
  // 初始化I2S喇叭
  i2s_config_t i2s_spk_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 64,
    .use_apll = false,
    .tx_desc_auto_clear = true,
    .fixed_mclk = 0
  };
  
  i2s_pin_config_t spk_pins = {
    .bck_io_num = I2S_SPK_BCLK,
    .ws_io_num = I2S_SPK_LRC,
    .data_out_num = I2S_SPK_DIN,
    .data_in_num = I2S_PIN_NO_CHANGE
  };
  
  i2s_driver_install(I2S_NUM_1, &i2s_spk_config, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &spk_pins);
  
  // 分配录音缓冲区
  audioBufferSize = SAMPLE_RATE * 2 * RECORD_SECONDS_MAX; // 16bit mono
  audioBuffer = (uint8_t*)malloc(audioBufferSize);
  
  Serial.println("✅ Charlie ESP32 节点就绪");
  Serial.println("👆 按住BOOT按钮开始说话，松开发送");
}

void loop() {
  static bool lastButtonState = HIGH;
  bool buttonState = digitalRead(BUTTON_PIN);
  
  // 按钮按下 → 开始录音
  if (buttonState == LOW && lastButtonState == HIGH) {
    startRecording();
  }
  // 按钮松开 → 停止录音并发送
  else if (buttonState == HIGH && lastButtonState == LOW) {
    stopRecording();
    sendToServer();
  }
  
  lastButtonState = buttonState;
  delay(50);
}

void startRecording() {
  if (isRecording) return;
  isRecording = true;
  audioBufferPos = 0;
  digitalWrite(LED_PIN, HIGH);
  Serial.println("🎤 开始录音...");
}

void stopRecording() {
  if (!isRecording) return;
  isRecording = false;
  digitalWrite(LED_PIN, LOW);
  Serial.printf("🎤 录音结束，共 %d 字节\n", audioBufferPos);
}

void sendToServer() {
  if (audioBufferPos < 1000) {
    Serial.println("⚠️ 录音太短，忽略");
    return;
  }
  
  Serial.println("📤 发送到服务器...");
  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "audio/wav");
  
  // 构建WAV头
  uint8_t wavHeader[44];
  buildWavHeader(wavHeader, audioBufferPos);
  
  // 发送音频: WAV头 + PCM数据
  http.setTimeout(30000);
  int httpCode = http.POST(wavHeader, 44 + audioBufferPos);
  
  if (httpCode == 200) {
    String response = http.getString();
    Serial.println("✅ 服务器响应已接收");
    // 解析响应并播放音频
    parseAndPlay(response);
  } else {
    Serial.printf("❌ 服务器错误: %d\n", httpCode);
  }
  http.end();
}

void buildWavHeader(uint8_t* header, size_t dataSize) {
  uint32_t sampleRate = SAMPLE_RATE;
  uint16_t bitsPerSample = 16;
  uint16_t channels = 1;
  uint32_t byteRate = sampleRate * channels * bitsPerSample / 8;
  
  memcpy(header, "RIFF", 4);
  *(uint32_t*)(header + 4) = 36 + dataSize;
  memcpy(header + 8, "WAVE", 4);
  memcpy(header + 12, "fmt ", 4);
  *(uint32_t*)(header + 16) = 16;
  *(uint16_t*)(header + 20) = 1; // PCM
  *(uint16_t*)(header + 22) = channels;
  *(uint32_t*)(header + 24) = sampleRate;
  *(uint32_t*)(header + 28) = byteRate;
  *(uint16_t*)(header + 32) = channels * bitsPerSample / 8;
  *(uint16_t*)(header + 34) = bitsPerSample;
  memcpy(header + 36, "data", 4);
  *(uint32_t*)(header + 40) = dataSize;
}

void parseAndPlay(String response) {
  // 从JSON响应中提取base64音频数据
  int audioStart = response.indexOf("\"audio\":\"");
  if (audioStart == -1) {
    Serial.println("⚠️ 响应中没有音频数据");
    return;
  }
  audioStart += 9;
  int audioEnd = response.indexOf("\"", audioStart);
  if (audioEnd == -1) return;
  
  String audioB64 = response.substring(audioStart, audioEnd);
  // 解码base64并播放（简化版，实际需要完整base64解码库）
  Serial.println("🔊 播放回复音频...");
  
  // 注意: 完整实现需要base64解码和MP3/WAV解码
  // 这里简化处理，ESP32内存有限，建议使用opus编码
  Serial.printf("📢 收到音频，长度: %d 字符\n", audioB64.length());
}
