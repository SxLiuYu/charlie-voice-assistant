# Charlie ESP32 固件 — PlatformIO 工程

遗留 `.ino`（HTTP POST 版）的正式工程化版本。真实硬件用 `esp32dev` 环境，
**没有硬件时用 `qemu` 环境**（Espressif QEMU fork，全本地仿真）。

## 环境

| 环境 | 用途 | 说明 |
|---|---|---|
| `esp32dev` | 真机烧录 | 原始代码路径，无任何仿真改动 |
| `qemu` | 模拟器开发 | `-DSIMULATION=1`，见下 |

```bash
# 编译（真机）
pio run -e esp32dev
# 编译（仿真）
pio run -e qemu
```

## QEMU 仿真

### 已验证的能力边界

| 能力 | QEMU 里 |
|---|---|
| bootloader → 固件启动 → Arduino setup/loop | ✅ 全链路 |
| GPIO（LED/按钮电平）、串口、heap 分配 | ✅ |
| 录音状态机 / 缓冲管理 / WAV 头构造 / JSON 解析 | ✅（SIM 分支） |
| WiFi / 网络栈 / HTTP | ❌ QEMU 不仿真 WiFi 外设（初始化即 Cache error） |
| I2S 麦克风采集 / 功放播放 | ❌ 无外设（SIM 用正弦波代替采样） |

### 用法

```bash
# 1. 编译 + 拼 flash 镜像（bootloader@0x1000 + partitions@0x8000 + boot_app0@0xe000 + app@0x10000）
pio run -e qemu
python make_flash_image.py   # 或按 README 里的 Python 片段手工拼

# 2. 跑仿真（串口输出到终端，~5 秒一轮模拟）
../qemu/qemu/bin/qemu-system-xtensa.exe -M esp32 \
    -drive file=flash.img,if=mtd,format=raw -nographic -no-reboot
```

### SIMULATION 模式改了什么

`-DSIMULATION=1` 时（`I2S_DRIVER_SUPPORTED=0`）：

1. `setup()` 跳过 WiFi/I2S 初始化（真机代码路径不变）
2. `loop()` 不读按钮，每 5 秒自动跑一轮「录音(正弦波 1 秒) → stopRecording → sendToServer」
3. `sendToServer()` 不调 HTTPClient（QEMU 无网络外设，一访问 WiFi 寄存器就
   Cache error），改为：构造并校验 WAV 头 → 用样例 JSON 跑 `parseAndPlay()`

真机构建（不带宏）不编译任何 SIM 代码，与原固件行为一致。

### 仿真抓出的真 bug（原 .ino 遗留）

`malloc(SAMPLE_RATE*2*10)` = 320KB——ESP32 单块堆最大约 110-160KB，必然返回
NULL，原代码不检查，第一次写录音数据就 StoreProhibited panic。
已修：分配失败自动降级（4s → 2s → 1s），全部失败才停机。

## Wokwi 备选（有虚拟 WiFi，可连真实服务端）

`../wokwi/bin/wokwi-cli.exe` 已就位（v0.29.1）。CLI 把固件上传到 Wokwi 云仿真，
需要 `WOKWI_CLI_TOKEN`（wokwi.com 注册 → Wokwi CI Dashboard 生成）。有 token 后：

```bash
set WOKWI_CLI_TOKEN=...
wokwi-cli ..\wokwi\project   # 无头跑，串口输出到终端
```

Wokwi 的优势是虚拟 WiFi 完整（能连真实 Charlie 服务端验证 HTTP 闭环），
且浏览器里可画 INMP441/MAX98357 接线图。QEMU 的优势是完全本地、零账号。
