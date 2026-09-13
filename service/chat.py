"""Kimi 联网对话：Moonshot 官方工具 web-search（Formula API）工具循环，纯 httpx，不经 openai 客户端。

用法：
    from .chat import ChatClient
    cc = ChatClient(config.config["chat"])
    cc.chat([{"role": "user", "content": "..."}])  -> {"answer": str, "used_search": bool}

密钥从 .env 读 MOONSHOT_API_KEY；模型/端点从 config.yml 的 chat 段读。

机制（Moonshot 官方工具 Formula API，比 $web_search 内建工具可靠得多）：
1. GET  /formulas/moonshot/web-search:latest/tools   拿工具声明（标准 function 类型）。
2. POST /chat/completions（tools=[声明]）→ 模型返回标准 function 类型的 tool_calls，
   arguments 形如 {"query":"...","classes":["academic"]}。
3. POST /formulas/moonshot/web-search:latest/fibers（body {"name","arguments"} 原样透传）
   → 返回 fiber，检索结果在 context.encrypted_output（----MOONSHOT ENCRYPTED BEGIN----...END----）。
4. 把 assistant(tool_calls) + role="tool"(content=encrypted_output) 回传 /chat/completions
   → 模型返回带真实引用的最终回答。模型可能多轮检索，每轮多次搜索，循环到 finish_reason=stop。

关键坑（2026-09 踩过）：
- 模型名已换代：kimi-k2-0711-preview 已下线，现为 kimi-k2.6 / kimi-k3 / kimi-k2.7-code。
- 必须【不要】关 thinking：设 "thinking":{"type":"disabled"} 时 kimi-k3 会跳过工具直接编造答案；
  不传 thinking（或 enabled）才会真正触发 web_search。这与 $web_search 内建工具相反。
- api.moonshot.cn 会【间歇性 SSL EOF】（[SSL: UNEXPECTED_EOF_WHILE_READING]，与 openai
  客户端无关，纯 httpx 也会撞上；国内共享 IP 链路不稳，同 arXiv 429）。所有请求统一走
  `_request()` 带指数退避重试，撞到 SSL/连接错误或 429/5xx 自动重试，不重试业务性 4xx。
- fibers 请求体是 {"name","arguments"}（arguments 为模型返回的原始 JSON 字符串），
  不要包成 {"tool_call":{...}}，也不要把 arguments 传成对象（都会 400）。
"""
import os
import time

import httpx

FORMULA_URI = "moonshot/web-search:latest"
MAX_TOOL_ROUNDS = 8  # 模型会多轮检索（每轮多次搜索），上限防死循环
RETRIES = 4          # 瞬态错误（SSL EOF / 连接 / 429 / 5xx）的重试次数
BACKOFF = 1.5        # 退避基数（秒），第 i 次重试前睡 BACKOFF * i
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ChatClient:
    def __init__(self, chat_cfg: dict):
        self.api_key = os.environ.get(chat_cfg.get("api_key_env") or "MOONSHOT_API_KEY")
        if not self.api_key:
            raise RuntimeError("环境变量 MOONSHOT_API_KEY 未设置，请在 .env 填写 Kimi key")
        self.base_url = (chat_cfg.get("base_url") or "https://api.moonshot.cn/v1").rstrip("/")
        self.model = chat_cfg.get("model") or "kimi-k3"
        self._headers = {"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"}
        self._tools: list[dict] | None = None  # 工具声明缓存

    def _request(self, method: str, url: str, *, json=None, timeout=120) -> httpx.Response:
        """带指数退避的请求：SSL EOF / 连接错误（httpx.HTTPError、OSError）与 429/5xx 重试。"""
        last_err: Exception | None = None
        for attempt in range(RETRIES):
            try:
                r = httpx.request(method, url, headers=self._headers, json=json, timeout=timeout)
                if r.status_code in _RETRYABLE_STATUS:
                    last_err = RuntimeError(f"Kimi 返回 {r.status_code}: {r.text[:200]}")
                else:
                    return r
            except (httpx.HTTPError, OSError) as e:
                last_err = e
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * (attempt + 1))
        raise last_err  # type: ignore[misc]

    def _get_tools(self) -> list[dict]:
        if self._tools is None:
            r = self._request("GET", f"{self.base_url}/formulas/{FORMULA_URI}/tools", timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"Kimi 工具声明获取失败 {r.status_code}: {r.text[:200]}")
            self._tools = r.json().get("tools") or []
        return self._tools

    def _create(self, messages: list[dict]) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": self._get_tools(),
            "max_tokens": 8192,
            # 不要设 "thinking": {"type":"disabled"}，否则模型不触发 web_search 而直接编造
        }
        r = self._request("POST", f"{self.base_url}/chat/completions", json=body, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"Kimi 返回 {r.status_code}: {r.text[:200]}")
        return r.json()

    def _run_fiber(self, fn: dict) -> str:
        """执行一次 web_search fiber，返回可回传给模型的 result 字符串。"""
        r = self._request("POST", f"{self.base_url}/formulas/{FORMULA_URI}/fibers",
                          json={"name": fn["name"], "arguments": fn["arguments"]}, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Kimi 搜索执行失败 {r.status_code}: {r.text[:200]}")
        ctx = r.json().get("context") or {}
        return ctx.get("output") or ctx.get("encrypted_output") or ""

    def chat(self, messages: list[dict]) -> dict:
        msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in messages]
        used_search = False
        for _ in range(MAX_TOOL_ROUNDS):
            data = self._create(msgs)
            choice = data["choices"][0]
            msg = choice["message"]
            if choice.get("finish_reason") != "tool_calls":
                return {"answer": msg.get("content") or "", "used_search": used_search}

            used_search = True
            tcs = msg.get("tool_calls") or []
            # 追加 assistant 消息（含 tool_calls，id 与下面 role=tool 一一对齐）
            msgs.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t["function"]["name"],
                                  "arguments": t["function"]["arguments"]}}
                    for t in tcs
                ],
            })
            # 每个 web_search 调用执行 fiber，把 encrypted_output 作为 tool 结果回传
            for t in tcs:
                result = self._run_fiber(t["function"])
                msgs.append({
                    "role": "tool",
                    "tool_call_id": t["id"],
                    "content": result,
                })
        return {"answer": "", "used_search": used_search}
