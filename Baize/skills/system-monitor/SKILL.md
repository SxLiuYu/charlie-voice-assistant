---
name: system-monitor
version: 1.0.0
description: "系统监控：CPU/内存/磁盘/负载/运行时间/网络状态（JARVIS式设备状态监控）"
capabilities:
  - system-monitor
  - system-info
risk_level: low
input_schema:
  type: object
  properties:
    action:
      type: string
      description: "all"
---

# 系统监控
查询当前设备/服务器状态，类似JARVIS监控Tony的实验室服务器。
