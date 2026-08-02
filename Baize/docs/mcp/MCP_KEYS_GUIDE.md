# 助手小子 · MCP 服务 Key 获取指南

> 现状：中国本土服务几乎无现成 MCP server，需用各平台开放 API 自行封装 MCP。
> 下表按"优先级 + 可行性"排序，标注了 Key 获取方式。

## 一、可立即申请（有开放 API，免费额度够用）

### 1. 高德地图 ★★★ 最优先
- **用途**：地理编码、路径规划、POI搜索、实时路况、天气
- **Key**：高德 Web 服务 Key
- **获取**：
  1. 注册高德开放平台 https://lbs.amap.com/
  2. 控制台 → 应用管理 → 创建新应用
  3. 添加 Key → 服务平台选"Web服务"
  4. 复制 Key
- **免费额度**：个人开发者每日 5000 次，够用
- **现成 MCP**：无官方，需自己封装（已有 SDK 可用，封装简单）
- **封装工作量**：小（1-2天）

### 2. 和风天气 ★★
- **用途**：天气预报、实时天气
- **Key**：API Key
- **获取**：
  1. 注册和风天气开发者 https://dev.qweather.com/
  2. 控制台 → 创建应用 → 获取 API Key
- **免费额度**：每日 1000 次（免费订阅版）
- **现成 MCP**：白泽已内置 weather 技能（用 wttr.in，无需 Key）
- **封装工作量**：极小

### 3. Tesla 车辆控制 ★★★（如你有特斯拉）
- **用途**：远程空调、门锁、充电状态、车辆定位
- **Key**：Tesla Fleet API Token + Client ID
- **获取**：
  1. 注册 Tesla Developer https://developer.tesla.com/
  2. 创建应用，获取 Client ID
  3. OAuth 授权流程获取车辆访问 Token
  4. 需要在 Tesla App 里给应用授权
- **现成 MCP**：GitHub 有第三方 Tesla MCP（需搜索 tesla mcp）
- **封装工作量**：中（OAuth 流程较复杂）
- **注意**：需 Tesla 账号及名下车辆

## 二、有开放 API 但以 B端为主（个人接入门槛高）

### 4. 滴滴打车 ★★
- **用途**：叫车、行程查询
- **Key**：滴滴开放平台 AppID + Secret
- **获取**：
  1. 滴滴开放平台 https://developer.didialift.com.cn/
  2. 注册开发者（需企业资质审核）
- **难点**：主要面向 B端/企业，个人开发者审核严格
- **现成 MCP**：无（中国打车无现成 MCP）
- **替代方案**：通过 App URL Scheme 唤起滴滴 App

### 5. 美团 / 大众点评 ★★
- **用途**：餐厅搜索、预订、外卖下单
- **Key**：美团开放平台 AppID
- **获取**：
  1. 美团开放平台 https://developer.meituan.com/
  2. 注册（企业资质）
- **难点**：C端"下单"API 不开放，开放的是商家端
- **现成 MCP**：无中国外卖 MCP
- **替代方案**：用美团 App 的 deeplink 跳转下单

### 6. 饿了么
- **用途**：外卖下单、配送查询
- **获取**：蜂鸟即配开放平台 https://open.ele.me/
- **难点**：主要是配送端 API，C端下单不开放
- **替代方案**：同上，deeplink 跳转

## 三、充电桩（开放程度低，需特殊处理）

### 7. 充电桩聚合 ★★
- **现状**：特来电、星星充电、e充电均无公开开放 API
- **可选方案**：
  - A. 用第三方聚合数据（如有）
  - B. 用 Open Charge Map（全球开源充电数据，偏欧美）
    - https://openchargemap.org/site/develop/api
    - GitHub 有现成 MCP：cyanheads/openchargemap-mcp-server
  - C. 抓取/自动化（灰色地带，不推荐）
- **推荐**：先用 Open Charge Map 的 MCP 跑通流程，中国充电桩数据后续对接

## 四、已现成可用的开源 MCP（无需自己开发）

| MCP Server | 功能 | 是否适用中国 |
|------------|------|-------------|
| Brave Search MCP | 网页搜索 | ✅ 需 Brave API Key |
| Fetch MCP | 抓取网页内容 | ✅ 无需 Key |
| Filesystem MCP | 本地文件操作 | ✅ 无需 Key |
| Memory MCP | 知识图谱记忆 | ✅ 无需 Key |
| Open Charge Map MCP | 全球充电桩 | ⚠️ 偏欧美数据 |
| osmmcp (OpenStreetMap) | 地图地理 | ⚠️ 数据不如高德全 |

## 五、推荐落地顺序（MVP → 完善）

### Phase 1：先跑通"出行"场景（1-2周）
1. ✅ 高德地图 Key（免费，当天申请到）→ 封装地图 MCP
2. ✅ 和风天气 Key（免费）→ 天气能力
3. 用这俩先做出行规划 Demo

### Phase 2：车辆 + 充电（2-4周）
4. Tesla Fleet API（如有车）→ 车控
5. Open Charge Map MCP（现成）→ 充电桩查询

### Phase 3：生活服务（需要平台合作或替代方案）
6. 美团/饿了么 → 用 deeplink 跳转方案（不调 API，直接唤起 App 下单）
7. 滴滴 → 同上 deeplink

### Phase 4：购物
8. 京东/淘宝开放平台（联盟 API）

## 六、给白泽加 MCP 网关的架构

白泽目前是"技能系统"不是"MCP协议"。两种融合方式：
- A. 给白泽写一个 MCP 适配层，把白泽技能包装成 MCP server/	client
- B. 在白泽旁挂一个独立 MCP 网关，白泽调用网关，网关调度各 MCP server

推荐 B：白泽保持现状，新增 `mcp-gateway` 模块，用 MCP Python/TS SDK 对接外部 server。
