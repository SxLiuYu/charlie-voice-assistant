"""诊断：把 1000 基准里"该调却没调对"和"误触发"的真实样本打出来，确认是 Laya
真弱点还是金标推导 bug。"""
import json
import os
import random
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(PROJECT_DIR, "logs", "calls_user.log")
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))

from laya_bench_1000 import load_real_tasks, derive_gold, evaluate, _COVERED_DOMAIN  # noqa
from laya_needle_bridge import LayaNeedleBridge

raw = load_real_tasks()
random.seed(42)
pool = random.choices(raw, k=1000)

b = LayaNeedleBridge()
b.warmup()

miss_calls, wrong_tools = [], []
n_call_gold = 0
from collections import Counter
miss_by_dom = Counter()
for u, dom in pool:
    in_scope, g_action, g_intent = derive_gold(u, dom)
    pred = b.handle(u)
    correct, fired_wrong, _ = evaluate(pred, in_scope, g_action, g_intent)
    if g_action == "call":
        n_call_gold += 1
        if not correct:
            miss_calls.append((u, dom, g_intent, pred.get("action"), pred.get("intent"),
                               round(pred.get("intent_conf") or 0, 3),
                               pred.get("needs_clarify")))
            miss_by_dom[dom] += 1
    if fired_wrong:
        wrong_tools.append((u, dom, pred.get("intent"), pred.get("name"),
                            round(pred.get("intent_conf") or 0, 3)))

print(f"in-scope call 金标总数={n_call_gold}, 漏/错={len(miss_calls)}")
print("漏/错按域:", dict(miss_by_dom))
print("\n===== 该调却没调对的样本（最多 24）=====")
for r in miss_calls[:24]:
    print(f"  话={r[0][:20]!r:22s} 金标域={r[1]:16s} 期望={r[2]:12s} "
          f"→ action={r[3]:8s} intent={str(r[4]):10s} conf={r[5]} clarify={r[6]}")

print(f"\n===== 误触发（发出错工具）样本（最多 18，共 {len(wrong_tools)}）=====")
for r in wrong_tools[:18]:
    print(f"  话={r[0][:22]!r:24s} 金标域={r[1]:16s} → bridge 调了 intent={r[2]:10s} name={r[3]} conf={r[4]}")
