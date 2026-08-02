---
name: jd-shopping
version: 1.0.0
description: "京东商品搜索：按关键词搜索京东商品，返回商品名+价格+店铺+佣金。需JD_APP_KEY/JD_APP_SECRET。goods.query权限开通后可用。"
capabilities:
  - jd-shopping
  - shopping
risk_level: low
input_schema:
  type: object
  properties:
    action:
      type: string
      description: "search(关键词搜索) / recommend(频道推荐)"
    keyword:
      type: string
      description: "搜索关键词"
    eliteId:
      type: number
      description: "频道ID，推荐模式用"
  required:
    - keyword
---

# 京东购物搜索
搜索京东商品，返回价格+店铺+销量。需 JD_APP_KEY 和 JD_APP_SECRET。
关键词搜索需 goods.query 权限，频道推荐用 material.query(已开通)。
