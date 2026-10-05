"""聊天会话存档：每次对话 = 一个会话（session），JSON 存到 chat-logs/，
并推送到「私有」ModelScope 仓库（paper-chat-logs）。缺 token / 未建仓时优雅降级只存本地。

会话文件格式（chat-logs/{session_id}.json）：
{
  "id": "…", "title": "首条用户消息（截断）", "model": "deepseek-chat",
  "created_at": "…", "updated_at": "…",
  "messages": [{"role": "user"|"assistant", "content": "…", "citations": [...]}]
}
「不含有思考」：上游传入的 messages 只含 {role, content}（思考/工具内部轮次已在上层丢弃）。
"""
import json
import re
from datetime import datetime
from pathlib import Path
from .storage import atomic_write


def _dir(config) -> Path:
    return config.root / "chat-logs"


def _safe_id(sid: str) -> str:
    """只保留文件名安全字符，防目录穿越 / 非法文件名。"""
    sid = re.sub(r"[^A-Za-z0-9._-]", "_", (sid or "").strip())[:80]
    return sid or datetime.now().strftime("%Y%m%d-%H%M%S")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _title_of(messages, fallback="新对话") -> str:
    for m in messages:
        if m.get("role") == "user":
            t = (m.get("content") or "").strip().replace("\n", " ")
            return t[:40] or fallback
    return fallback


def _clean_messages(messages) -> list:
    out = []
    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        item = {"role": role, "content": (m.get("content") or "")}
        if m.get("citations"):
            item["citations"] = m["citations"]
        if m.get("tool_events"):
            item["tool_events"] = m["tool_events"]
        out.append(item)
    return out


def save_session(session_id, messages, config, model=None, title=None,
                 created_at=None) -> dict:
    """写/覆盖一个会话文件并推送私有云端；返回 {id, path, pushed, title}。"""
    sid = _safe_id(session_id)
    msgs = _clean_messages(messages)
    d = _dir(config)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.json"

    if created_at is None and path.exists():
        try:
            created_at = json.loads(path.read_text(encoding="utf-8")).get("created_at")
        except Exception:
            created_at = None
    data = {
        "id": sid,
        "title": title or _title_of(msgs),
        "model": model or "deepseek-chat",
        "created_at": created_at or _now(),
        "updated_at": _now(),
        "messages": msgs,
    }
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))

    pushed = False
    try:
        from .cloud import push_chat_logs
        pushed = bool(push_chat_logs("chore: 保存聊天记录"))
    except Exception as e:
        print(f"[chatlog] 推送失败（已存本地）: {e}")
    return {"id": sid, "path": str(path), "pushed": pushed, "title": data["title"]}


def list_sessions(config) -> list:
    """列出全部会话（按更新时间倒序），只返回元数据（不含消息体）。"""
    d = _dir(config)
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "id": data.get("id", p.stem),
                "title": data.get("title") or "（无标题）",
                "model": data.get("model"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "turns": sum(1 for m in data.get("messages", []) if m.get("role") == "user"),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out


def load_session(session_id, config) -> dict:
    sid = _safe_id(session_id)
    p = _dir(config) / f"{sid}.json"
    if not p.exists():
        return {"error": "会话不存在或已被删除"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "会话文件损坏"}


def delete_session(session_id, config) -> dict:
    sid = _safe_id(session_id)
    p = _dir(config) / f"{sid}.json"
    if not p.exists():
        return {"deleted": False, "error": "会话不存在"}
    p.unlink()
    pushed = False
    try:
        from .cloud import push_chat_logs
        pushed = bool(push_chat_logs("chore: 删除聊天记录"))
    except Exception as e:
        print(f"[chatlog] 删除推送失败: {e}")
    return {"deleted": True, "id": sid, "pushed": pushed}


def save_and_sync(messages, config, model=None) -> dict:
    """兼容旧接口（CLI 手动保存）：用时间戳作会话 id 存一份，等价于 save_session。"""
    return save_session(datetime.now().strftime("%Y%m%d-%H%M%S"),
                        messages, config, model=model)
