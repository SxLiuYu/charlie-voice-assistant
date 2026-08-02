---
name: deeplink
version: 1.0.0
description: "生成外卖/购物/打车/餐厅的App跳转链接。用户说出需求，生成美团/饿了么/淘宝/京东/滴滴的搜索链接，点击直接跳转App下单。"
capabilities:
  - deeplink
  - waimai
  - shopping
  - ride
  - food
risk_level: low
input_schema:
  type: object
  properties:
    intent:
      type: string
      description: "意图: waimai(外卖) / food(餐厅) / shopping(购物) / grocery(买菜) / pharmacy(买药) / ride(打车)"
    keyword:
      type: string
      description: "搜索关键词，如：蛋糕、感冒药、火锅"
    location:
      type: string
      description: "位置（可选）"
  required:
    - intent
    - keyword
---

# App 跳转链接生成

根据用户需求生成对应平台的搜索/下单跳转链接。
- 外卖 → 美团外卖/饿了么
- 餐厅 → 大众点评/美团
- 购物 → 淘宝/京东/拼多多
- 打车 → 滴滴
