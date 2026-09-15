"""设置控制台：读取/写入 knowledge-base/config.yml 与 .env，并触发运行中服务的重载。

设计要点：
- config.yml 只读写「原始未解析」内容，保留 ${ZOTERO_USER_ID} 等占位符（避免把
  占位符解析成明文后回写、丢失 .env 间接引用）。
- API 密钥一律写 .env（KEY=value），config.yml 里只存指向 .env 的占位符/变量名。
- 写入后调用 load_dotenv(override=True) 刷新 os.environ，再由调用方重置 web 的
  _CONFIG / _DOCS 缓存，使新配置对后续请求生效。
"""
import yaml
from dotenv import dotenv_values, load_dotenv, set_key

from .config_loader import ROOT

CONFIG_PATH = ROOT / "knowledge-base" / "config.yml"
ENV_PATH = ROOT / ".env"

# 可编辑的 config.yml 字段：(点分路径, 中文标签, 类型 text|number|list)
# 只暴露「代码真正消费」的字段；路径类（cache_dir/inbox_dir/index_dir）与
# embedding 模型名等改动需重建索引，故不在此暴露，避免运行时改坏。
CONFIG_FIELDS = [
    ("model.default", "主模型", "text"),
    ("model.base_url", "主模型 base_url", "text"),
    ("model.tasks.classify", "分类任务模型", "text"),
    ("model.tasks.summarize", "摘要任务模型", "text"),
    ("model.tasks.judge", "裁判任务模型", "text"),
    ("model.tasks.librarian", "馆长任务模型", "text"),
    ("chat.base_url", "联网对话 base_url", "text"),
    ("chat.model", "联网对话模型", "text"),
    ("storage.cloud.namespace", "ModelScope 命名空间", "text"),
    ("storage.cloud.pdf_repo", "PDF 仓库", "text"),
    ("storage.cloud.kb_repo", "知识库仓库", "text"),
    ("retrieval.top_k", "检索返回条数", "number"),
    ("retrieval.min_sim", "最低相似度", "number"),
    ("retrieval.hybrid.dense", "语义权重 dense", "number"),
    ("retrieval.hybrid.title_boost", "标题关键词加权", "number"),
    ("retrieval.explore.lambda", "探索 MMR lambda", "number"),
    ("quality.weights.venue", "评分权重·会议", "number"),
    ("quality.weights.relevance", "评分权重·相关", "number"),
    ("quality.weights.citation", "评分权重·引用", "number"),
    ("quality.weights.recency", "评分权重·新近", "number"),
    ("quality.weights.novelty", "评分权重·新颖", "number"),
    ("quality.weights.llm_judge", "评分权重·LLM 裁判", "number"),
    ("daily.top_k", "每日检索条数", "number"),
    ("daily.recent_days", "新鲜度窗口(天)", "number"),
    ("daily.per_query", "每查询×每源上限", "number"),
    ("daily.min_relevance", "最低相关度", "number"),
    ("daily.sources", "检索源(每行一个)", "list"),
    ("daily.queries", "兴趣点查询(每行一个)", "list"),
    ("open_original.order", "跳转顺序(每行一个)", "list"),
]

# 可编辑的 .env 密钥：(变量名, 中文标签, 是否密钥 secret)
ENV_FIELDS = [
    ("DEEPSEEK_API_KEY", "DeepSeek API Key", True),
    ("MOONSHOT_API_KEY", "Kimi (Moonshot) API Key", True),
    ("MODELSCOPE_TOKEN", "ModelScope Token", True),
    ("ZOTERO_USER_ID", "Zotero 用户 ID", False),
    ("ZOTERO_API_KEY", "Zotero API Key", True),
    ("SMTP_HOST", "SMTP 服务器", False),
    ("SMTP_PORT", "SMTP 端口", False),
    ("SMTP_USER", "SMTP 用户名(邮箱)", False),
    ("SMTP_PASS", "SMTP 授权码", True),
    ("SMTP_TO", "收件人邮箱", False),
]


def _load_raw_config() -> dict:
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    return {}


def _get_path(d: dict, path: str):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _set_path(d: dict, path: str, value):
    keys = path.split(".")
    cur = d
    for key in keys[:-1]:
        if not isinstance(cur.get(key), dict):
            cur[key] = {}
        cur = cur[key]
    cur[keys[-1]] = value


def _mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "••••" + v[-2:]
    return v[:4] + "…" + v[-4:]


def get_settings() -> dict:
    """返回给设置页的数据：config 字段（含当前值）+ env 字段（含是否已配置/掩码）。"""
    raw = _load_raw_config()
    config_fields = []
    for path, label, ftype in CONFIG_FIELDS:
        value = _get_path(raw, path)
        if isinstance(value, list):
            value = "\n".join(str(x) for x in value)
        config_fields.append({
            "path": path, "label": label, "type": ftype,
            "value": "" if value is None else value,
        })

    env_values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    env_fields = []
    for key, label, secret in ENV_FIELDS:
        val = env_values.get(key, "") or ""
        env_fields.append({
            "key": key, "label": label, "secret": secret,
            "set": bool(val), "masked": _mask(val),
        })

    return {"config_fields": config_fields, "env_fields": env_fields}


def _coerce(ftype: str, value):
    if ftype == "number":
        if value in (None, ""):
            return None
        try:
            f = float(value)
            return int(f) if f == int(f) else f
        except (TypeError, ValueError):
            return value
    if ftype == "list":
        if isinstance(value, str):
            return [x.strip() for x in value.splitlines() if x.strip()]
        if isinstance(value, list):
            return [str(x) for x in value]
        return value
    return value


def apply_settings(config_updates: dict, env_updates: dict) -> dict:
    """把设置写回 config.yml 与 .env，并刷新 os.environ。返回结果摘要。"""
    saved = []

    # 1) config.yml：合并到原始未解析内容，保留 ${VAR} 占位符
    if config_updates:
        raw = _load_raw_config()
        type_map = {path: ftype for path, _label, ftype in CONFIG_FIELDS}
        for path, value in config_updates.items():
            ftype = type_map.get(path, "text")
            value = _coerce(ftype, value)
            if value is None:
                continue
            _set_path(raw, path, value)
            saved.append(path)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False,
                           default_flow_style=False),
            encoding="utf-8")

    # 2) .env：set_key 只改目标行、保留其余注释/行
    if env_updates:
        for key, value in env_updates.items():
            # None / 空串 = 清除该项；非空 = 覆盖
            set_key(str(ENV_PATH), key, "" if value in (None, "") else str(value))
            saved.append(key)

    # 3) 刷新环境变量（override=True 覆盖已 set 的旧值）
    load_dotenv(ENV_PATH, override=True)

    return {"ok": True, "saved": saved, "restart": False}
