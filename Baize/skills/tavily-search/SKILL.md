---
name: tavily-search
version: 1.0.0
description: "网页搜索：用Tavily API搜索互联网信息、新闻、知识。适合查实时信息、资料。"
capabilities:
  - tavily-search
  - web-search
risk_level: low
input_schema:
  type: object
  properties:
    query:
      type: string
      description: "搜索关键词"
    max_results:
      type: number
      description: "返回数量，默认5"
  required:
    - query
---

# Tavily 网页搜索
搜索互联网获取实时信息。需配置 `TAVILY_API_KEY`。
用法：query="北京天气"，max_results=5
