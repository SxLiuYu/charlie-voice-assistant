/**
 * API 服务 - OpenClaw 风格
 */

import express, { Request, Response } from 'express';
import cors from 'cors';
import { getBrainV3 } from '../core/brain-v3';
import { getSkillRegistry } from '../skills/registry';
import { getEnhancedMemory } from '../memory/v3';
import { initDatabase } from '../memory/database';
import { getCostManager } from '../core/cost';
import { getLLMManager } from '../llm';
import { getLogger } from '../observability/logger';
import { SkillLoader } from '../skills/loader';
import { registerBuiltinSkills } from '../skills/builtins';

const logger = getLogger('api');

// 主动提醒推送：在线客户端连接集合（JARVIS式主动播报）
const reminderClients = new Set<Response>();

let initialized = false;

/**
 * 初始化 API 服务
 */
async function initializeAPI(): Promise<void> {
  if (initialized) return;

  try {
    // 初始化数据库
    await initDatabase();

    // 初始化 LLM
    getLLMManager();

    // 注册内置技能
    registerBuiltinSkills();

    // 加载外部技能
    const loader = new SkillLoader();
    const skills = await loader.loadAll();
    const registry = getSkillRegistry();
    for (const skill of skills) {
      registry.register(skill);
    }

    initialized = true;
    logger.info('API 服务初始化完成');
  } catch (error) {
    logger.error('API 服务初始化失败', { error });
    throw error;
  }
}

/**
 * 创建 API 服务器
 */

// ==================== 意图直连路由 ====================
// 检测明确意图直接调用技能，保证返回结构化结果（而非打开浏览器）
async function routeIntent(message: string): Promise<string | null> {
  const registry = getSkillRegistry();
  const m = message;
  
  // 提取地名："我在望京" → 望京
  const addrMatch = m.match(/我在\s*([^\s，,。的]+)/);
  const address = addrMatch ? addrMatch[1] : '';

  // POI搜索类：充电桩/餐厅/停车场等
  const poiTypes = ['充电桩','充电站','餐厅','停车场','加油站','咖啡','酒店','超市','商场','医院','药店','银行','厕所'];
  const poiKeyword = poiTypes.find(t => m.includes(t));
  if (poiKeyword && /附近|周围|找|查|搜|哪里|哪有|有没有/.test(m)) {
    const skill = registry.get('amap');
    if (skill) {
      const r = await skill.run({ action: 'poi_search', keyword: poiKeyword, address: address || '北京', radius: 3000 }, {});
      if (r.success) return r.message || '未找到结果';
    }
  }
  
  // 天气
  if (/天气|气温|多少度|下雨|冷不冷|热不热/.test(m)) {
    const skill = registry.get('amap');
    if (skill) {
      const r = await skill.run({ action: 'weather', city: address || '北京' }, {});
      if (r.success) return r.message || '天气查询失败';
    }
  }
  
  // 路径规划："从A到B"
  const routeMatch = m.match(/从(.+?)[到去](.+?)(怎么走|路线|导航|多远|多久)/);
  if (routeMatch) {
    const skill = registry.get('amap');
    if (skill) {
      // 先地理编码起终点
      const startR = await skill.run({ action: 'geocode', address: routeMatch[1] }, {});
      const endR = await skill.run({ action: 'geocode', address: routeMatch[2] }, {});
      if (startR.success && endR.success) {
        const startLoc = (startR.data as any)?.location;
        const endLoc = (endR.data as any)?.location;
        if (startLoc && endLoc) {
          const r = await skill.run({ action: 'route', origin: startLoc, destination: endLoc }, {});
          if (r.success) return r.message || '路线规划失败';
        }
      }
    }
  }
  
  // 网页搜索
  if (/搜一下|搜索一下|查一下资料|帮我查|帮我搜/.test(m)) {
    const skill = registry.get('tavily-search');
    if (skill) {
      const query = m.replace(/帮我|搜一下|搜索一下|查一下|资料/g, '').trim();
      const r = await skill.run({ query }, {});
      if (r.success) return r.message || '搜索失败';
    }
  }
  
  // 外卖点餐
  if (/外卖|点个|点餐|饿了么|美团外卖|帮.*点|订.*饭|点份|点一份/.test(m)) {
    const skill = registry.get('deeplink');
    if (skill) {
      const whatMatch = m.match(/(?:点|订|买|来)个?(.+?)(?:吧|呗|的|了|$)/);
      const what = whatMatch ? whatMatch[1].replace(/[吧呗的了]/g,'').trim() : '';
      const r = await skill.run({ intent: 'waimai', keyword: what }, {});
      if (r.success) return r.message || '';
    }
  }
  
  // 智能购物推荐：优先京东实时价格，403/失败降级到搜评测+LLM筛选
  if (/帮我找|帮我选|推荐.*买|买.*推荐|哪个.*好|性价比|值得买/.test(m)) {
    // 关键词提取：去掉所有修饰词，剩下的就是商品名
    const item = m.replace(/帮我|帮|找|选|买|推荐|个|款|性价比|值得买|哪个|哪个好|的|了|吧|呗|高|低|便宜|好|怎么样|什么|请问|一下|来|下|最|非常|特别/g, '').trim();
    if (item) {
      // 1) 优先：京东关键词搜索（实时价格+销量，需 goods.query 权限）
      const jdSkill = registry.get('jd-shopping');
      if (jdSkill) {
        try {
          const jdR = await jdSkill.run({ action: 'search', keyword: item }, {});
          if (jdR.success && jdR.message) {
            const goods = (jdR.data as unknown as any[]) || [];
            if (goods.length > 0) {
              const llm = getLLMManager();
              const aiR = await llm.chat([
                { role: 'system', content: '你是专业购物顾问。从下面京东搜到的"' + item + '"商品里，综合价格、销量、店铺评分、是否自营，选出最值得买的前5个，保留原文商品名和价格。格式：1.【商品名】¥价格 推荐理由(一句话)' },
                { role: 'user', content: goods.slice(0, 10).map((g: any, i: number) => `${i+1}.${g.name} ¥${g.price}${g.couponPrice ? '(券后¥'+g.couponPrice+')' : ''} ${g.isSelf ? '自营' : ''} ${g.shop} 评分${g.shopLevel} 30天售${g.sales30}件 评论${g.comments}`).join('\n') }
              ], { temperature: 0.3 }, 'aliyun');
              return aiR.content || jdR.message;
            }
          }
        } catch (e) {
          logger.warn('jd-shopping 搜索失败，降级到 Tavily', { error: (e as Error).message });
        }
      }
      // 2) 主方案：双源搜索（什么值得买查价格 + 通用查评测）+ LLM 提取真商品真价格
      const tavily = registry.get('tavily-search');
      const [priceR, reviewR] = await Promise.all([
        tavily.run({ query: item + ' 价格 推荐', max_results: 5, search_depth: 'advanced', include_domains: ['smzdm.com'] }, {}),
        tavily.run({ query: item + ' 评测 推荐 型号 性价比', max_results: 5, search_depth: 'advanced' }, {}),
      ]);
      const priceDocs = (priceR.success && priceR.data) ? (priceR.data as unknown as any[]) : [];
      const reviewDocs = (reviewR.success && reviewR.data) ? (reviewR.data as unknown as any[]) : [];
      if (priceDocs.length || reviewDocs.length) {
        const docs = [
          ...priceDocs.map((d: any, i: number) => `[价格${i+1}] ${d.title}\n${d.content || ''}`),
          ...reviewDocs.map((d: any, i: number) => `[评测${i+1}] ${d.title}\n${d.content || ''}`),
        ].join('\n\n').slice(0, 10000);
        const llm = getLLMManager();
        const aiR = await llm.chat([
          { role: 'system', content: '你是专业购物顾问。用户想买【' + item + '】。从下面搜索结果中提取与' + item + '直接相关的具体产品型号和真实价格，优先用[价格]来源里的商品和报价。只列有明确型号和价格的商品，凑齐5个最优，每个写一句话推荐理由。格式：1.【产品型号】¥XX 推荐理由' },
          { role: 'user', content: '用户想买：' + item + '\n\n以下是搜索到的商品价格和评测信息：\n' + docs }
        ], { temperature: 0.3 }, 'aliyun');
        return aiR.content || '分析失败';
      }
    }
  }
  
  // 购物
  if (/买个|买点|购物|淘宝|京东|拼多多|下单|买一个/.test(m)) {
    const skill = registry.get('deeplink');
    if (skill) {
      const whatMatch = m.match(/(?:买|购|下单)(?:个|点|一个)?(.+?)(?:吧|呗|的|了|$)/);
      const what = whatMatch ? whatMatch[1].replace(/[吧呗的了]/g,'').trim() : '';
      const r = await skill.run({ intent: 'shopping', keyword: what }, {});
      if (r.success) return r.message || '';
    }
  }
  
  // 餐厅/美食
  if (/餐厅|吃饭|美食|团购|订餐|附近.*吃|好吃|推荐.*吃/.test(m)) {
    const skill = registry.get('deeplink');
    if (skill) {
      const whatMatch = m.match(/(?:吃|找|推荐|搜)(.+?)(?:吧|呗|的|了|店|餐厅|$)/);
      const what = whatMatch ? whatMatch[1].trim() : '美食';
      const r = await skill.run({ intent: 'food', keyword: what }, {});
      if (r.success) return r.message || '';
    }
  }
  
  // 打车
  if (/打车|叫车|滴滴|出租车|网约车|帮我叫|打个车/.test(m)) {
    const skill = registry.get('deeplink');
    if (skill) {
      const r = await skill.run({ intent: 'ride', keyword: '' }, {});
      if (r.success) return r.message || '';
    }
  }
  
  // 买菜/买药
  if (/买菜|生鲜/.test(m)) {
    const skill = registry.get('deeplink');
    if (skill) {
      const whatMatch = m.match(/(?:买|来)(.+?)(?:吧|呗|的|了|$)/);
      const what = whatMatch ? whatMatch[1].trim() : '蔬菜';
      const r = await skill.run({ intent: 'grocery', keyword: what }, {});
      if (r.success) return r.message || '';
    }
  }
  if (/买药|药店|感冒药|创可贴|退烧药/.test(m)) {
    const skill = registry.get('deeplink');
    if (skill) {
      const whatMatch = m.match(/(?:买|来)(.+?)(?:吧|呗|的|了|$)/);
      const what = whatMatch ? whatMatch[1].trim() : '感冒药';
      const r = await skill.run({ intent: 'pharmacy', keyword: what }, {});
      if (r.success) return r.message || '';
    }
  }
  // ==================== JARVIS能力包 ====================
  // 翻译
  if (/翻译|translate/.test(m)) {
    const llm = getLLMManager();
    let target = '英文';
    if (/翻译成.*(中文|汉语)/.test(m)) target = '中文';
    else if (/翻译成.*(日文|日语)/.test(m)) target = '日文';
    else if (/翻译成.*(韩文|韩语)/.test(m)) target = '韩文';
    // 优先取引号内内容
    const quoted = m.match(/["""''\u201c]([^"""''\u201d]+)["""''\u201d]/);
    let content = quoted ? quoted[1] : m.replace(/翻译成?(?:英文|英语|中文|汉语|日文|日语|韩文|韩语)?|translate|帮我把|把|这个|这句话|这段|翻译一下|翻译|成|为/g, '').trim();
    if (content) {
      const aiR = await llm.chat([
        { role: 'system', content: '你是专业翻译引擎。将用户给出的内容翻译成' + target + '。只输出译文，不加解释和前后缀。' },
        { role: 'user', content }
      ], { temperature: 0.2 }, 'aliyun');
      return aiR.content || '翻译失败';
    }
  }

  // 时间/日期
  if (/现在几点|今天星期几|今天几号|今天日期|什么时间|现在什么时间|几号了|星期几|几月几号/.test(m)) {
    const now = new Date();
    const w = ['日', '一', '二', '三', '四', '五', '六'][now.getDay()];
    return `🕐 现在是 ${now.toLocaleString('zh-CN', { hour12: false })}，星期${w}`;
  }

  // 计算 / 单位换算
  if (/^\s*[\d.\s+\-*/().%]+\s*[=＝]?\s*$/.test(m) || /计算|算一下|等于多少|换算|是多少/.test(m)) {
    const expr = m.replace(/计算|算一下|等于|多少|换算|帮|我|是多少|\s*[=＝]?\s*$/g, '').trim();
    if (expr && /^[\d.\s+\-*/().%]+$/.test(expr)) {
      try {
        const r = Function('"use strict";return (' + expr.replace(/\^/g, '**') + ')')();
        return `🔢 ${expr} = ${r}`;
      } catch { /* 落到LLM */ }
    }
    const llm = getLLMManager();
    const aiR = await llm.chat([
      { role: 'system', content: '你是计算与换算助手。直接计算或换算，输出结果和一行过程。' },
      { role: 'user', content: m }
    ], { temperature: 0 }, 'aliyun');
    return aiR.content || '计算失败';
  }

  // 系统监控
  if (/系统状态|系统信息|CPU|内存占用|磁盘空间|设备状态|服务器状态|系统监控|电脑状态/.test(m)) {
    const skill = registry.get('system-monitor');
    if (skill) { const r = await skill.run({ action: 'all' }, {}); if (r.success) return r.message || ''; }
  }

  // 待办/提醒
  if (/提醒我|添加待办|新增待办|加个待办|记一下要|记个要/.test(m)) {
    const skill = registry.get('todo');
    if (skill) {
      const text = m.replace(/提醒我|添加待办|新增待办|加个待办|记一下要|记个要|帮|我|的|了|吧/g, '').trim();
      const tm = text.match(/((?:明天|后天|大后天|今天|\d+\s*(?:分钟后|小时后|天后)|上午|下午|晚上)?[^，,。]*(?:\d+\s*[点时:：]\s*\d{0,2}\s*分?|分钟|小时后|天后))/);
      const todoText = (tm ? text.replace(tm[1], '') : text).trim() || text;
      const timeStr = tm ? tm[1].trim() : '';
      const r = await skill.run({ action: 'add', text: todoText, time: timeStr }, {});
      if (r.success) return r.message || '';
    }
  }
  if (/我的待办|待办清单|有什么待办|待办列表|查看待办|列出待办|待办事项/.test(m)) {
    const skill = registry.get('todo');
    if (skill) { const r = await skill.run({ action: 'list' }, {}); if (r.success) return r.message || ''; }
  }
  if (/完成.*待办|完成第|搞定第|做完第|删掉第.*待办/.test(m)) {
    const skill = registry.get('todo');
    if (skill) {
      const idx = m.match(/第?(\d+)/);
      const r = await skill.run({ action: 'done', index: idx ? idx[1] : '1' }, {});
      if (r.success) return r.message || '';
    }
  }

  return null;
}

export function createAPIServer(options: { port: number } = { port: 3000 }) {
  const app = express();
  
  app.use(cors());
  app.use(express.json());

  // ==================== 健康检查 ====================
  
  app.get('/health', (req: Request, res: Response) => {
    res.json({ 
      status: 'ok', 
      timestamp: Date.now(),
      version: '3.2.0'
    });
  });

  // ==================== 对话接口 ====================

  // 对话接口（非流式）
  app.post('/api/chat', async (req: Request, res: Response) => {
    try {
      const { message, conversationId = 'default' } = req.body;
      
      if (!message) {
        return res.status(400).json({ error: 'message is required' });
      }

      logger.info('对话请求', { message: message.slice(0, 50), conversationId });

      // 确保初始化
      await initializeAPI();

      // 意图直连：明确意图直接调技能返回结果，而非打开浏览器
      const routed = await routeIntent(message);
      if (routed) {
        return res.json({
          success: true,
          data: { type: 'skill_direct', response: routed, conversationId, confidence: 1 },
        });
      }
      const brain = getBrainV3();
      const decision = await brain.process(message);

      res.json({
        success: true,
        data: {
          type: decision.intent?.type || 'unknown',
          response: decision.response,
          intent: decision.intent,
          conversationId,
          confidence: decision.confidence,
        },
      });
    } catch (error) {
      logger.error('对话处理失败', { error });
      res.status(500).json({
        success: false,
        error: error instanceof Error ? error.message : '未知错误',
      });
    }
  });

  // 流式对话接口
  app.post('/api/chat/stream', async (req: Request, res: Response) => {
    try {
      const { message, conversationId = 'default' } = req.body;
      
      if (!message) {
        return res.status(400).json({ error: 'message is required' });
      }

      // 确保初始化
      await initializeAPI();

      // 设置 SSE 头
      res.setHeader('Content-Type', 'text/event-stream');
      res.setHeader('Cache-Control', 'no-cache');
      res.setHeader('Connection', 'keep-alive');
      res.setHeader('X-Accel-Buffering', 'no');

      const brain = getBrainV3();
      
      for await (const event of brain.processStream(message, conversationId)) {
        res.write(`event: ${event.type}\n`);
        res.write(`data: ${JSON.stringify(event.data)}\n\n`);
      }

      res.end();
    } catch (error) {
      logger.error('流式对话处理失败', { error });
      if (!res.headersSent) {
        res.status(500).json({
          success: false,
          error: error instanceof Error ? error.message : '未知错误',
        });
      }
    }
  });

  // 获取对话历史
  app.get('/api/chat/history', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const memory = getEnhancedMemory();
      const stats = memory.getStats();
      res.json({
        success: true,
        data: {
          totalMemories: stats.totalMemories,
          byType: stats.byType,
          conversationId: 'default'
        }
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // 清空对话历史
  app.delete('/api/chat/history', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const memory = getEnhancedMemory();
      memory.clearWorkingMemory();
      res.json({
        success: true,
        message: '工作记忆已清空'
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // ==================== 技能接口 ====================

  // 获取技能列表
  app.get('/api/skills', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const registry = getSkillRegistry();
      const skills = registry.getAll().map(s => s.toInfo());
      res.json({
        success: true,
        data: { skills }
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // 获取技能详情
  app.get('/api/skills/:name', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const registry = getSkillRegistry();
      const skillName = Array.isArray(req.params.name) ? req.params.name[0] : req.params.name;
      const skill = registry.get(skillName);
      if (!skill) {
        return res.status(404).json({ error: '技能不存在' });
      }
      res.json({
        success: true,
        data: skill.toInfo()
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // ==================== 记忆接口 ====================

  // 获取记忆统计
  app.get('/api/memory/stats', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const memory = getEnhancedMemory();
      const stats = memory.getStats();
      res.json({
        success: true,
        data: stats
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // 搜索记忆
  app.get('/api/memory/search', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const query = req.query.q;
      if (!query || typeof query !== 'string') {
        return res.status(400).json({ error: 'q is required' });
      }
      const memory = getEnhancedMemory();
      const results = await memory.queryFacts(query, 10);
      res.json({
        success: true,
        data: { results }
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // ==================== 成本接口 ====================

  // 获取成本统计
  app.get('/api/cost/stats', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const costManager = getCostManager();
      const stats = costManager.getStats();
      res.json({
        success: true,
        data: stats
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // 获取成本配置
  app.get('/api/cost/config', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const costManager = getCostManager();
      res.json({
        success: true,
        data: costManager.getConfig()
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // 更新成本配置
  app.put('/api/cost/config', async (req: Request, res: Response) => {
    try {
      await initializeAPI();
      const costManager = getCostManager();
      costManager.updateConfig(req.body);
      res.json({
        success: true,
        message: '配置已更新'
      });
    } catch (error) {
      res.status(500).json({ error: String(error) });
    }
  });

  // ==================== 主动提醒推送 (JARVIS式主动播报) ====================
  app.get('/api/reminders/stream', async (req: Request, res: Response) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    reminderClients.add(res);
    res.write(': connected\n\n');
    req.on('close', () => { reminderClients.delete(res); });
  });

  // 提醒调度器：每30秒检查到期待办，主动推送给在线客户端（无在线客户端时不消费提醒）
  let reminderStarted = false;
  function startReminderScheduler() {
    if (reminderStarted) return;
    reminderStarted = true;
    setInterval(async () => {
      if (reminderClients.size === 0) return;
      try {
        await initializeAPI();
        if (reminderClients.size === 0) return;
        const registry = getSkillRegistry();
        const skill = registry.get('todo');
        if (!skill) return;
        const r = await skill.run({ action: 'check' }, {});
        if (r.success && r.message) {
          const payload = 'event: reminder\ndata: ' + JSON.stringify({ message: r.message }) + '\n\n';
          for (const client of reminderClients) {
            try { client.write(payload); } catch {}
          }
          logger.info('主动提醒已推送', { clients: reminderClients.size });
        }
      } catch (e) {
        logger.warn('提醒调度异常', { error: (e as Error).message });
      }
    }, 30000);
  }
  startReminderScheduler();

  // ==================== 配置接口 ====================

  // 获取 LLM 配置
  app.get('/api/config/llm', (req: Request, res: Response) => {
    res.json({
      success: true,
      data: {
        default: 'aliyun',
        providers: ['aliyun', 'zhipu', 'ollama']
      }
    });
  });

  return {
    app,
    start: () => {
      app.listen(options.port, '0.0.0.0', () => {
        logger.info(`API服务已启动: http://0.0.0.0:${options.port}`);
      });
    },
    stop: () => {
      logger.info('API服务已停止');
    },
  };
}

export type APIServer = ReturnType<typeof createAPIServer>;
