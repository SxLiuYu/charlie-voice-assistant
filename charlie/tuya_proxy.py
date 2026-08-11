"""Tuya API Proxy — 为 Charlie 和 HA 提供 Tuya 设备控制接口"""
import os, json
from fastapi import APIRouter, HTTPException
from tuya_api import TuyaAPI

router = APIRouter(prefix="/api/tuya", tags=["tuya"])

AC_DEVICE_ID = os.getenv("TUYA_AC_DEVICE_ID", "")   # 空调遥控器 remote_id
IR_DEVICE_ID = os.getenv("TUYA_IR_DEVICE_ID", "")  # 红外网关 infrared_id

def _get_2c_api():
    """2C 终端用户 API(设备列表/详情/通用属性下发), 需 TUYA_API_KEY"""
    if not os.getenv("TUYA_API_KEY"):
        raise HTTPException(503, "TUYA_API_KEY 未配置")
    return TuyaAPI()

def _get_2b_api():
    """2B 开发者红外云 API(空调红外发码), 需 TUYA_CLIENT_ID/TUYA_ACCESS_KEY"""
    try:
        from tuya_api import TuyaCloudAPI
        return TuyaCloudAPI()
    except ValueError:
        raise HTTPException(503, "TUYA_CLIENT_ID/TUYA_ACCESS_KEY (2B) 未配置")

@router.get("/devices")
def list_devices():
    """列出所有 Tuya 设备"""
    api = _get_2c_api()
    return api.get_all_devices()

@router.get("/devices/{device_id}")
def get_device(device_id: str):
    """获取设备详情"""
    api = _get_2c_api()
    return api.get_device_detail(device_id)

@router.post("/devices/{device_id}/control")
def control_device(device_id: str, properties: dict):
    """控制设备（发送属性命令）"""
    api = _get_2c_api()
    return api.issue_properties(device_id, properties)

@router.get("/ac")
def ac_status():
    """获取空调状态(2B 红外遥控器最后下发的状态)"""
    api = _get_2b_api()
    s = api.ac_status(IR_DEVICE_ID, AC_DEVICE_ID)
    return s

@router.post("/ac/control")
def ac_control(action: str, temperature: int = 0, fan_speed: str = ""):
    """控制空调(2B 红外云 API, 真正触发红外发码)
    - action: on/off/cool/heat/auto/fan/dry
    - temperature: 16-30
    - fan_speed: auto/low/medium/high
    """
    api = _get_2b_api()
    act = action.lower()
    mode_map = {"cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4}
    if act == "off":
        power, mode = 0, None
    elif act in mode_map:
        power, mode = 1, mode_map[act]
    elif act == "on":
        power, mode = 1, None
    else:
        raise HTTPException(400, f"不支持的动作: {action}")
    eff_temp = max(16, min(30, temperature if temperature > 0 else 26))
    wind = None
    if fan_speed:
        fan_map = {"auto": 0, "low": 1, "medium": 2, "high": 3}
        if fan_speed in fan_map:
            wind = fan_map[fan_speed]
        else:
            raise HTTPException(400, f"不支持的风速: {fan_speed}")
    return api.ac_scenes_command(IR_DEVICE_ID, AC_DEVICE_ID, power=power, mode=mode, temp=eff_temp, wind=wind)
