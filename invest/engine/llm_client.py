#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容 /chat/completions），零第三方依赖（标准库 urllib）。

配置优先级：环境变量 > llm_config.json
  LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

用法：
  from llm_client import chat, chat_json, mask
  txt  = chat("你是分析师", "写一段话")
  data = chat_json(system, user)        # 强制解析出 dict/list

安全：任何日志/异常都只打印掩码后的 key（mask()），绝不输出明文。
"""
import os, sys, json, time, re, urllib.request, urllib.error

# 让本模块既可作为 invest.engine.llm_client 被导入，也可直接 python 运行自测
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from invest import paths

HERE = os.path.dirname(os.path.abspath(__file__))
# 配置改落 invest/config/llm_config.json（含密钥，已由 .gitignore 排除）
CFG_PATH = str(paths.CONFIG_DIR / "llm_config.json")


def load_cfg(path=None):
    cfg = {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "timeout": 120,
        "max_retries": 3,
        "temperature": 0.4,
    }
    p = path or CFG_PATH
    if os.path.exists(p):
        try:
            cfg.update({k: v for k, v in json.load(open(p, encoding="utf-8")).items()
                        if not k.startswith("_")})
        except Exception as e:
            print(f"[llm] 配置文件解析失败: {e}", file=sys.stderr)
    # 环境变量覆盖
    for env, key in (("LLM_BASE_URL", "base_url"), ("LLM_API_KEY", "api_key"), ("LLM_MODEL", "model")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["base_url"] = str(cfg.get("base_url", "")).rstrip("/")
    return cfg


def mask(key):
    """掩码密钥，仅用于日志。"""
    if not key:
        return "<empty>"
    return f"{key[:3]}***{key[-2:]}" if len(key) > 8 else "***"


def available(path=None):
    cfg = load_cfg(path)
    return bool(cfg.get("api_key")) and bool(cfg.get("base_url"))


def chat(system, user, cfg=None, temperature=None, max_tokens=None,
         json_mode=False, verbose=True):
    """单轮对话，返回文本内容。失败抛异常（已重试）。"""
    cfg = cfg or load_cfg()
    key = cfg.get("api_key")
    if not key:
        raise RuntimeError("未配置 api_key（llm_config.json 或环境变量 LLM_API_KEY）")

    url = cfg["base_url"] + "/chat/completions"
    payload = {
        "model": cfg.get("model"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": cfg.get("temperature", 0.4) if temperature is None else temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    retries = int(cfg.get("max_retries", 3))
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=int(cfg.get("timeout", 120))) as r:
                raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw)
            if "choices" not in data:
                raise RuntimeError(f"响应无 choices: {str(data)[:300]}")
            msg = data["choices"][0].get("message") or {}
            content = msg.get("content") or ""
            if not content and msg.get("reasoning_content"):
                content = msg["reasoning_content"]
            usage = data.get("usage") or {}
            if verbose:
                print(f"[llm] ok model={cfg.get('model')} "
                      f"tok_in={usage.get('prompt_tokens')} tok_out={usage.get('completion_tokens')}",
                      file=sys.stderr)
            return content.strip()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            last = RuntimeError(f"HTTP {e.code}: {detail}")
            # 4xx（除 429）无需重试
            if e.code not in (408, 409, 425, 429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = e
        if i < retries - 1:
            wait = 2 * (i + 1)
            print(f"[llm] 第{i+1}次失败({last})，{wait}s 后重试 …", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"LLM 调用失败(key={mask(key)}, base={cfg.get('base_url')}): {last}")


_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def _extract_json(text):
    """从可能带 markdown 围栏 / 前后废话的文本中抠出第一个完整 JSON。"""
    t = (text or "").strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 括号配平扫描
    for op, cl in (("{", "}"), ("[", "]")):
        st = t.find(op)
        if st < 0:
            continue
        depth, instr, esc = 0, False, False
        for i in range(st, len(t)):
            ch = t[i]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch == op:
                depth += 1
            elif ch == cl:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[st:i + 1])
                    except Exception:
                        break
    raise ValueError(f"无法从响应中解析 JSON：{(text or '')[:300]}")


def chat_json(system, user, cfg=None, temperature=0.3, max_tokens=None, retries=2):
    """要求模型输出 JSON，返回 python 对象。自动重试 + 容错解析。"""
    cfg = cfg or load_cfg()
    sys_j = system + "\n\n【输出格式】只输出一个合法 JSON，不要任何解释文字、不要 markdown 围栏。"
    last = None
    for i in range(retries + 1):
        try:
            txt = chat(sys_j, user, cfg=cfg, temperature=temperature,
                       max_tokens=max_tokens, json_mode=(i == 0))
            return _extract_json(txt)
        except Exception as e:
            last = e
            # json_mode 可能不被端点支持 → 第二次关掉再试
            print(f"[llm] JSON 解析/调用失败({str(e)[:120]})，重试 {i+1}/{retries}", file=sys.stderr)
    raise RuntimeError(f"chat_json 失败: {last}")


if __name__ == "__main__":
    cfg = load_cfg()
    print(f"base_url = {cfg.get('base_url')}")
    print(f"model    = {cfg.get('model')}")
    print(f"api_key  = {mask(cfg.get('api_key'))}")
    print("--- 连通性测试 ---")
    try:
        out = chat("你是简洁的助手。", "只回复两个字：连通")
        print("文本响应:", out[:120])
    except Exception as e:
        print("文本调用失败:", e)
        sys.exit(1)
    print("--- JSON 模式测试 ---")
    try:
        d = chat_json("你是数据助手。", '返回 {"ok": true, "msg": "hello"}')
        print("JSON 响应:", d)
    except Exception as e:
        print("JSON 模式失败:", e)
