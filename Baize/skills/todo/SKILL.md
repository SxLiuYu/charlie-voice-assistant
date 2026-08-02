---
name: todo
version: 1.0.0
description: "待办事项与提醒：添加待办、列出清单、完成任务、检查到期提醒（JARVIS式主动提醒）"
capabilities:
  - todo
  - reminder
  - task-management
risk_level: low
input_schema:
  type: object
  properties:
    action:
      type: string
      description: "add/list/done/check"
    text:
      type: string
    time:
      type: string
      description: "提醒时间，如'明天上午9点''30分钟后''14:30'"
    index:
      type: number
  required:
    - action
---

# 待办与提醒
JARVIS式任务管理。支持自然语言时间（明天/后天/X小时后/X点等）。
- add: 添加待办+可选提醒时间
- list: 列出未完成项
- done: 按序号完成
- check: 检查到期提醒（供主动轮询）
