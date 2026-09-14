"""联网对话存档：把 clean 的 user/assistant 对话写 Markdown 到 knowledge-base/chat-logs/，
并推送到 ModelScope KB 仓库（幂等；缺 token / 未初始化 git 时优雅降级，只存本地）。

「不含有思考」：上游传入的 messages 只含 {role, content}（思考/工具内部轮次已在上层丢弃）。
"""
from datetime import datetime
from pathlib import Path


def _dir(config) -> Path:
    return config.root / "knowledge-base" / "chat-logs"


def _render(messages, model) -> str:
    dt = datetime.now()
    user_turns = sum(1 for m in messages if m.get("role") == "user")
    lines = [
        "---",
        f"date: {dt.isoformat(timespec='seconds')}",
        f"model: {model}",
        f"turns: {user_turns}",
        "---",
        "",
        "# 联网对话存档",
        "",
    ]
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "user":
            lines += ["## 用户", "", content, ""]
        elif role == "assistant" and content:
            lines += ["## AI", "", content, ""]
    return "\n".join(lines)


def save(messages, config, model=None) -> Path:
    """写一份存档文件，返回本地路径。"""
    d = _dir(config)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = d / f"{stamp}.md"
    i = 2
    while path.exists():
        path = d / f"{stamp}_{i}.md"
        i += 1
    path.write_text(_render(messages, model or "kimi-k3"), encoding="utf-8")
    return path


def save_and_sync(messages, config) -> dict:
    """写存档并尝试推送 ModelScope；返回 {path, pushed}。"""
    model = (config.config.get("chat") or {}).get("model") or "kimi-k3"
    path = save(messages, config, model)
    pushed = False
    try:
        from .cloud import push_kb
        pushed = bool(push_kb("chore: 保存联网对话"))
    except Exception as e:
        print(f"[chatlog] 推送失败（已存本地）: {e}")
    return {"path": str(path), "pushed": pushed}
