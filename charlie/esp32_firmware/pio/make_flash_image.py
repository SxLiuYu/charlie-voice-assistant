"""拼接 ESP32 QEMU flash 镜像（pio run -e qemu 之后执行）"""
import os

build = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pio", "build", "qemu")
home = os.path.expanduser("~")
boot_app0 = os.path.join(home, ".platformio", "packages",
                         "framework-arduinoespressif32", "tools", "partitions", "boot_app0.bin")
img = bytearray(4 * 1024 * 1024)
for off, path in [(0x1000, os.path.join(build, "bootloader.bin")),
                  (0x8000, os.path.join(build, "partitions.bin")),
                  (0xE000, boot_app0),
                  (0x10000, os.path.join(build, "firmware.bin"))]:
    with open(path, "rb") as f:
        data = f.read()
    img[off:off + len(data)] = data
    print(f"{os.path.basename(path)} -> 0x{off:X} ({len(data)} bytes)")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flash.img")
with open(out, "wb") as f:
    f.write(img)
print("flash.img:", os.path.getsize(out), "bytes")
