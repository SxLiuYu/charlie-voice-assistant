"""共享依赖: 各子 MCP 服务器 import 此模块, 避免代码重复"""
import os, sys, requests
from dotenv import load_dotenv

os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

ALIYUN = os.getenv("ALIYUN_API_KEY", "")
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
ESP32_IP = os.getenv("ESP32_IP", "")
NCM_BIN = os.path.expanduser("~/.local/bin/ncm")
# PyInstaller 打包后优先从 _internal/bin/ncm 找
if getattr(sys, 'frozen', False):
    _ncm_packed = os.path.join(os.path.dirname(sys.executable), '_internal', 'bin', 'ncm')
    if os.path.isfile(_ncm_packed):
        NCM_BIN = _ncm_packed


def aliyun_chat(messages, temperature=0.3):
    """调用阿里云 qwen-max (翻译/计算等场景共用)

    未配置 ALIYUN_API_KEY 时快速失败，且错误消息**引导主模型自己完成任务**：
    工具异常文本会原样交给 LLM，若写成"翻译服务不可用"，主模型会先向用户
    复述"服务不可用"再自己给出答案 —— 用户听到两段矛盾的话。引导语让主模型
    跳过报错、直接产出结果。
    """
    if not ALIYUN:
        raise RuntimeError(
            "aliyun 未配置。请直接自己完成用户的这个任务（如翻译/换算/计算），"
            "不要提及本错误或任何服务状态。")
    r = requests.post(DASHSCOPE, headers={"Authorization": f"Bearer {ALIYUN}", "Content-Type": "application/json"},
        json={"model": "qwen-max", "messages": messages, "temperature": temperature, "stream": False}, timeout=60)
    return r.json()["choices"][0]["message"]["content"]


def _safe_math_eval(expr: str) -> float | None:
    """安全的数学表达式求值(基于AST, 不使用eval)"""
    import ast, operator
    operators = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.USub: operator.neg, ast.UAdd: operator.pos, ast.FloorDiv: operator.floordiv,
    }
    def _eval(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"不允许的常量类型: {type(node.value)}")
        elif isinstance(node, ast.BinOp):
            left = _eval(node.left); right = _eval(node.right)
            op_type = type(node.op)
            if op_type in operators:
                return operators[op_type](left, right)
            raise ValueError(f"不允许的操作: {op_type.__name__}")
        elif isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            op_type = type(node.op)
            if op_type in operators:
                return operators[op_type](operand)
            raise ValueError(f"不允许的一元操作: {op_type.__name__}")
        else:
            raise ValueError(f"不允许的语法: {type(node).__name__}")
    try:
        tree = ast.parse(expr, mode='eval')
        return _eval(tree.body)
    except Exception:
        return None


def _ensure_https(url: str) -> str:
    """强制 https:// 避免 HTTPS 页面加载 HTTP 资源被浏览器阻止(混合内容)"""
    if url.startswith("http://"):
        return "https://" + url[7:]
    elif url.startswith("//"):
        return "https:" + url
    return url
