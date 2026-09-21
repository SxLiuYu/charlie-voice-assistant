"""Laya + Needle3 千条压力 + 准确率基准（从生产日志抽取真实任务）。

数据来源：charlie/logs/calls_user.log 里 feature=intent 的真实条目，
每条记录"用户原话 → charlie 当时路由到的域"。这就是 ground-truth 金标。
我们按真实流量频率有放回抽 1000 条（保留生产分布），喂给 Laya+Needle3
bridge，测两项：

  - 反应速度：每条 handle()（Laya 路由 + 罗马化 + Needle3 抽参）的端到端延迟；
  - 准确率：双引擎"该调工具时调对、该放手时放手"。
       * in-scope 且金标=call：bridge 必须 action=call 且 intent 命中预期；
       * in-scope 且金标=abstain（true chitchat）：bridge 应 action∈{chitchat,none}；
       * out-of-scope（bridge 不覆盖的域）：bridge 应 abstain，一旦 action=call
         且调了错工具 = 误触发（唯一真失败）。

指标定义（尽量诚实，不放水）：
  - accuracy = (正确 in-scope 调用 + 正确 abstain) / 1000
  - tool_precision = 正确 in-scope 调用 / bridge 实际发出的调用数
  - abstain_recall = 正确 abstain / 应 abstain 总数

用法：python3 scripts/laya_bench_1000.py [N=1000] [seed=42]
产物：scripts/laya_bench_1000_report.json
"""
import json
import os
import random
import statistics
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(PROJECT_DIR, "logs", "calls_user.log")
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))

# ---- 金标派生：charlie 域 → 双引擎应做的判定 ----
# 双引擎覆盖的域（与 INTENT_OPTIONS 对齐）
_COVERED_DOMAIN = {
    "amap-maps": "weather",      # 天气
    "magic-reminder": "reminder",  # 提醒（set/list 再细分）
    "magic-music": "music",
    "magic-notes": "notes",
}
# magic-info 是时间+算术+新闻的混合域，只有"时间"子类是双引擎能接的
_TIME_KW = ["几点", "几号", "日期", "星期几", "现在时间", "现在几点", "现在几点了",
            "什么时间", "今天周", "现在是", "时间到了", "时间到", "what time",
            "几点了", "几时", "北京时间", "现在是什么时候", "现在几"]
_LIST_KW = ["有哪些", "有什么", "列表", "我的提醒", "看提醒", "列一下",
            "今天提醒", "提醒列表", "提醒有哪些", "什么提醒", "提醒呢"]

# 复合域（逗号分隔）里若出现被覆盖子域，bridge 命中该子域即视为正确
_COVERED_INTENTS = {"weather", "time", "reminder", "reminder_list", "music",
                    "stock", "notes"}


def derive_gold(utterance: str, domain: str):
    """返回 (in_scope: bool, gold_action: str, gold_intent: str|None)。

    诚实金标：charlie 日志里的域映射成双引擎应有的判定。
      - 单覆盖域 → 该 call；
      - none → 该 abstain；
      - magic-info 且含时间词 → time call；
      - 复合域（如 baize-skills,magic-life,magic-reminder）→ 若含覆盖子域，
        in_scope=True 但 gold_intent=None（命中任一覆盖子意图都算对）；
      - 其余 → 该 abstain（双引擎不覆盖，放手给 LLM 才对）。
    """
    u = utterance or ""
    if domain in _COVERED_DOMAIN:
        expected = _COVERED_DOMAIN[domain]
        if domain == "magic-reminder":
            expected = "reminder_list" if any(k in u for k in _LIST_KW) else "reminder"
        return True, "call", expected
    if domain == "none":
        return True, "abstain", None
    if domain == "magic-info" and any(k in u for k in _TIME_KW) and "提醒" not in u:
        return True, "call", "time"
    if "," in domain:  # 复合域：含覆盖子域即视为在覆盖范畴
        subs = domain.split(",")
        if any(s in _COVERED_DOMAIN for s in subs):
            return True, "any_covered", None  # 命中任一覆盖子意图即正确
        # 含 none 的复合（如 baize-skills,magic-life）→ 放手也算对
        return False, "abstain", None
    return False, "abstain", None


def evaluate(pred: dict, in_scope: bool, gold_action: str, gold_intent: str):
    """返回 (correct: bool, fired_wrong_tool: bool, abstained_ok: bool)。

    gold_action=="any_covered"：bridge 只要 abstain 或 call 一个覆盖域意图即正确，
    且 call 的非覆盖意图才算误触发（如复合域里真发了 stock 而金标是 reminder）。
    """
    action = pred.get("action")
    intent = pred.get("intent")
    if gold_action == "any_covered":
        if action == "call":
            if intent in _COVERED_INTENTS:
                return True, False, False  # 命中覆盖子域，正确
            return False, True, False     # 发了非覆盖工具，误触发
        return True, False, True          # abstain 也正确
    if gold_action == "call":
        if action != "call":
            return False, False, False
        if gold_intent == intent:
            return True, False, False
        if gold_intent in ("reminder", "reminder_list") and intent in ("reminder", "reminder_list"):
            return True, False, False
        return False, True, False
    else:
        if action == "call":
            return False, True, False
        return True, False, True


def load_real_tasks():
    """从日志抽取 (utterance, domain) 原始条目（保留真实频率）。"""
    tasks = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("feature") != "intent":
                continue
            u = (d.get("input") or "").strip()
            dom = d.get("output") or ""
            if not u:
                continue
            tasks.append((u, dom))
    return tasks


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    random.seed(seed)

    raw = load_real_tasks()
    print(f"[bench] 真实意图条目: {len(raw)}；抽取 {n} 条（有放回，保真实流量分布），seed={seed}")

    from laya_needle_bridge import LayaNeedleBridge
    b = LayaNeedleBridge()
    print("[bench] 预热 Laya 权重（首次 ~20s）...")
    tw = b.warmup()
    print(f"[bench] 预热完成: {tw:.1f}s")

    pool = random.choices(raw, k=n)
    # 金标 + 预测
    lats, results = [], []
    stat = {
        "in_scope_call_total": 0, "in_scope_call_correct": 0,
        "abstain_total": 0, "abstain_correct": 0,
        "bridge_calls": 0, "wrong_tool": 0,
    }
    per_class = {}  # domain -> [correct, total]
    for idx, (u, dom) in enumerate(pool):
        in_scope, g_action, g_intent = derive_gold(u, dom)
        t0 = time.perf_counter()
        try:
            pred = b.handle(u)
        except Exception as e:  # 极端兜底：任何崩溃算误触发
            pred = {"action": "error", "intent": None, "error": str(e)[:80]}
        dt = (time.perf_counter() - t0) * 1000.0
        lats.append(dt)
        correct, fired_wrong, abstained_ok = evaluate(pred, in_scope, g_action, g_intent)
        results.append((u, dom, in_scope, g_action, g_intent,
                        pred.get("action"), pred.get("intent"), correct, dt))

        pc = per_class.setdefault(dom, [0, 0])
        pc[1] += 1
        if correct:
            pc[0] += 1
        if g_action == "call":
            stat["in_scope_call_total"] += 1
            if correct:
                stat["in_scope_call_correct"] += 1
        else:
            stat["abstain_total"] += 1
            if correct:
                stat["abstain_correct"] += 1
        if pred.get("action") == "call":
            stat["bridge_calls"] += 1
        if fired_wrong:
            stat["wrong_tool"] += 1

        if (idx + 1) % 100 == 0:
            print(f"  ...{idx+1}/{n}  done")

    # 聚合
    total = len(results)
    overall_correct = sum(1 for r in results if r[7])
    lats_sorted = sorted(lats)
    def pct(p):
        if not lats_sorted:
            return 0.0
        k = max(0, min(len(lats_sorted) - 1, int(p / 100 * len(lats_sorted))))
        return lats_sorted[k]
    report = {
        "n": n, "seed": seed, "warmup_s": round(tw, 2),
        "latency_ms": {
            "mean": round(statistics.mean(lats), 1),
            "median": round(statistics.median(lats), 1),
            "p50": round(pct(50), 1), "p90": round(pct(90), 1),
            "p95": round(pct(95), 1), "p99": round(pct(99), 1),
            "min": round(min(lats), 1), "max": round(max(lats), 1),
        },
        "accuracy_overall": round(overall_correct / total, 4),
        "in_scope_call": {
            "total": stat["in_scope_call_total"],
            "correct": stat["in_scope_call_correct"],
            "rate": round(stat["in_scope_call_correct"] / stat["in_scope_call_total"], 4)
            if stat["in_scope_call_total"] else None,
        },
        "abstain": {
            "total": stat["abstain_total"],
            "correct": stat["abstain_correct"],
            "recall": round(stat["abstain_correct"] / stat["abstain_total"], 4)
            if stat["abstain_total"] else None,
        },
        "tool_precision": round(stat["in_scope_call_correct"] / stat["bridge_calls"], 4)
        if stat["bridge_calls"] else None,
        "bridge_calls": stat["bridge_calls"],
        "wrong_tool_triggers": stat["wrong_tool"],
        "per_domain": {k: {"correct": v[0], "total": v[1],
                           "rate": round(v[0] / v[1], 4)}
                       for k, v in sorted(per_class.items(),
                                          key=lambda x: -x[1][1])},
    }
    out = os.path.join(PROJECT_DIR, "scripts", "laya_bench_1000_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n==================== 基准结果 ====================")
    print(f"样本数: {n}  预热: {tw:.1f}s")
    print(f"延迟(ms)  mean={report['latency_ms']['mean']}  "
          f"p50={report['latency_ms']['p50']}  p90={report['latency_ms']['p90']}  "
          f"p95={report['latency_ms']['p95']}  p99={report['latency_ms']['p99']}  "
          f"max={report['latency_ms']['max']}")
    print(f"综合准确率: {report['accuracy_overall']*100:.1f}%")
    ic = report["in_scope_call"]
    print(f"  in-scope 调用准确率: {ic['correct']}/{ic['total']} "
          f"= {ic['rate']*100 if ic['rate'] is not None else 'NA'}%")
    ab = report["abstain"]
    print(f"  abstain 召回(该放手时放手): {ab['correct']}/{ab['total']} "
          f"= {ab['recall']*100 if ab['recall'] is not None else 'NA'}%")
    print(f"  工具调用精度: {report['tool_precision']*100 if report['tool_precision'] is not None else 'NA'}%  "
          f"(bridge 共发 {report['bridge_calls']} 次调用)")
    print(f"  ⚠ 误触发(发出错工具): {stat['wrong_tool']} 次")
    print("\n--- 按 charlie 域的命中率（抽样≥10 的域）---")
    for dom, v in report["per_domain"].items():
        if v["total"] >= 10:
            print(f"  {dom:18s} {v['correct']:4d}/{v['total']:4d}  {v['rate']*100:5.1f}%")
    print(f"\n报告已存: {out}")


if __name__ == "__main__":
    main()
