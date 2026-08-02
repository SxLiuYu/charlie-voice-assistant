---
name: amap
version: 1.0.0
description: "高德地图服务：地理编码、POI搜索、路径规划、天气查询。用于查充电桩、餐厅、规划路线、看天气。"
capabilities:
  - amap
  - map
  - geocode
  - poi_search
  - route_planning
  - weather
risk_level: low
input_schema:
  type: object
  properties:
    action:
      type: string
      description: "操作类型: geocode(地址转坐标) / poi_search(POI搜索) / route(路径规划) / weather(天气)"
    keyword:
      type: string
      description: "POI搜索关键词，如：充电桩、餐厅、停车场"
    location:
      type: string
      description: "中心坐标 lng,lat 如 116.48,39.99"
    address:
      type: string
      description: "地址文本，如 北京市朝阳区"
    radius:
      type: number
      description: "搜索半径(米)，默认3000"
    origin:
      type: string
      description: "路径起点坐标"
    destination:
      type: string
      description: "路径终点坐标"
    city:
      type: string
      description: "城市adcode或名称，如 110105 或 北京"
  required:
    - action
---

# 高德地图服务

提供地图核心能力，需配置环境变量 `AMAP_API_KEY`。

## 操作示例

- 查充电桩：action=poi_search, keyword=充电桩, location=116.48,39.99
- 地址转坐标：action=geocode, address=北京市朝阳区
- 规划路线：action=route, origin=116.48,39.99, destination=116.43,39.90
- 查天气：action=weather, city=110105
