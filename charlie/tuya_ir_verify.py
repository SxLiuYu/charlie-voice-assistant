#!/usr/bin/env python3
"""Tuya 2B 红外空调控制验证脚本

验证 2B 开发者红外云 API 能否真正控制空调 (2C shadow API 无法发码, 见 RTK 已知问题)。
前提: .env 配置 TUYA_CLIENT_ID + TUYA_ACCESS_KEY (涂鸦云应用 Access ID/Key,
      与 2C 的 TUYA_API_KEY 不同套) + TUYA_AC_DEVICE_ID (红外网关设备 ID)。
用法: python tuya_ir_verify.py [status|on|off]
  status (默认): 只读 — 换 token + 列遥控器 + 找空调 remote_id (不发码, 安全)
  on:  下发开机 制冷 26°C 低风 (power=1,mode=0,temp=26,wind=1)
  off: 下发关机 (power=0)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from tuya_api import TuyaCloudAPI, TuyaAPIError


def main():
    # infrared_id = 红外网关设备(TUYA_IR_DEVICE_ID), 非空调遥控器(TUYA_AC_DEVICE_ID)
    did = os.getenv("TUYA_IR_DEVICE_ID", "")
    if not did:
        print("TUYA_IR_DEVICE_ID (红外网关) 未配置")
        sys.exit(1)
    try:
        api = TuyaCloudAPI()  # 从 env 读 2B 凭证
    except ValueError as e:
        print(f"2B 凭证缺失: {e}")
        print("请在 .env 配置 TUYA_CLIENT_ID 和 TUYA_ACCESS_KEY (云应用 Access ID/Key)")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    # 1) 换 token (验证签名)
    try:
        api._refresh_token()
        print(f"[1/3] token OK, expire @ {api._token_expire:.0f}")
    except TuyaAPIError as e:
        print(f"[1/3] token 失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[1/3] token 异常: {e}")
        sys.exit(1)

    # 2) 列遥控器, 找空调 remote_id (category_id == 5)
    try:
        remotes = api.get_ir_remotes(did)
    except TuyaAPIError as e:
        print(f"[2/3] 列遥控器失败: {e}")
        sys.exit(1)
    print(f"[2/3] remotes({len(remotes) if isinstance(remotes, list) else '?'}): {remotes}")

    remote_id = None
    if isinstance(remotes, list):
        for r in remotes:
            if r.get("category_id") == 5:
                remote_id = r.get("remote_id")
                print(f"  -> 空调 remote_id={remote_id} brand={r.get('brand_name')}")
                break
    if not remote_id:
        print("[2/3] 未找到空调遥控器 (category_id==5)。请先在涂鸦 App 配对空调。")
        sys.exit(1)

    # 3) 下发命令
    if cmd == "on":
        try:
            ok = api.ac_scenes_command(did, remote_id, power=1, mode=0, temp=26, wind=1)
            print(f"[3/3] ON result={ok} (True=红外码已发, 空调应响应)")
        except TuyaAPIError as e:
            print(f"[3/3] ON 失败: {e}")
            sys.exit(1)
    elif cmd == "off":
        try:
            ok = api.ac_scenes_command(did, remote_id, power=0)
            print(f"[3/3] OFF result={ok}")
        except TuyaAPIError as e:
            print(f"[3/3] OFF 失败: {e}")
            sys.exit(1)
    else:
        print(f"[3/3] status 模式: remote_id={remote_id}, 用 'on'/'off' 实际控制")
        print("  示例: python tuya_ir_verify.py on   # 开机制冷26度低风")


if __name__ == "__main__":
    main()
