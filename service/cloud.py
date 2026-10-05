"""云端同步：python -m service.cloud [sync]

把 cache/ 的 PDF（git-lfs）、knowledge-base/ 的 Markdown、chat-logs/ 的聊天记录增量推送到
ModelScope 数据集仓库（paper-library-pdfs + paper-knowledge-base 公开，paper-chat-logs 私有）。
幂等：无变更则跳过；token 从 .env 读 MODELSCOPE_TOKEN。
"""
import os
import subprocess
import sys

from dotenv import load_dotenv

from .config_loader import Config, ROOT


def _git(repo_dir, *args):
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                          capture_output=True, text=True)


def _remote_url(namespace, repo, token):
    return f"https://oauth2:{token}@www.modelscope.cn/datasets/{namespace}/{repo}.git"


def _ensure_remote(repo_dir, namespace, repo, token):
    want = _remote_url(namespace, repo, token)
    cur = _git(repo_dir, "remote", "get-url", "origin").stdout.strip()
    if cur == want:
        return
    _git(repo_dir, "remote", "set-url" if cur else "add", "origin", want)


def _sync_repo(repo_dir, namespace, repo, token, message):
    if not (repo_dir / ".git").exists():
        print(f"[cloud] {repo} 尚未初始化 git，跳过")
        return False
    _git(repo_dir, "config", "user.name", os.environ.get("GIT_USER_NAME", "you"))
    _git(repo_dir, "config", "user.email", os.environ.get("GIT_USER_EMAIL", "you@example.com"))
    _ensure_remote(repo_dir, namespace, repo, token)
    _git(repo_dir, "add", "-A")
    if not _git(repo_dir, "status", "--porcelain").stdout.strip():
        print(f"[cloud] {repo} 无变更，跳过")
        return True
    _git(repo_dir, "commit", "-m", message,
         "-m", "Co-Authored-By: Claude Code <noreply@anthropic.com>")
    p = subprocess.run(["git", "-C", str(repo_dir), "push", "-u", "origin", "master"],
                       capture_output=True, text=True)
    tail = (p.stderr or "").strip()[-300:]
    print(f"[cloud] {repo} push 完成" + (f"\n{tail}" if p.returncode else ""))
    return p.returncode == 0


def _ctx():
    """返回 (namespace, token, cfg)；缺 token 时打印并返回 None。"""
    load_dotenv(ROOT / ".env")
    token = os.environ.get("MODELSCOPE_TOKEN")
    if not token:
        print("[cloud] 缺 MODELSCOPE_TOKEN（.env）")
        return None
    return Config(), token


def sync():
    got = _ctx()
    if not got:
        raise RuntimeError("未配置 MODELSCOPE_TOKEN")
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    namespace = cloud["namespace"]
    _sync_repo(cfg.cache_dir, namespace, cloud["pdf_repo"].split("/")[-1], token,
               "chore: 同步论文 PDF（git-lfs）")
    _sync_repo(ROOT / "knowledge-base", namespace, cloud["kb_repo"].split("/")[-1], token,
               "chore: 同步双语知识库")
    push_chat_logs("chore: 同步聊天记录")


def push_kb(message: str = "chore: 保存联网对话") -> bool:
    """只推送 knowledge-base，返回是否成功；缺 token 返回 False。"""
    got = _ctx()
    if not got:
        return False
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    return _sync_repo(ROOT / "knowledge-base", cloud["namespace"],
                      cloud["kb_repo"].split("/")[-1], token, message)


def pull():
    """从 ModelScope 拉取两个仓库最新到本地（严格绑定的『读』侧）。"""
    got = _ctx()
    if not got:
        raise RuntimeError("未配置 MODELSCOPE_TOKEN")
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    namespace = cloud["namespace"]
    for repo_dir, repo in [(cfg.cache_dir, cloud["pdf_repo"].split("/")[-1]),
                           (ROOT / "knowledge-base", cloud["kb_repo"].split("/")[-1]),
                           (ROOT / "chat-logs", _chat_repo_name(cloud))]:
        if not (repo_dir / ".git").exists():
            print(f"[cloud] {repo} 尚未初始化 git，跳过 pull")
            continue
        _ensure_remote(repo_dir, namespace, repo, token)
        p = _git(repo_dir, "pull", "origin", "master")
        line = (p.stdout or p.stderr or "").strip().replace("\n", " ")
        print(f"[cloud] {repo} pull: {line[-160:]}")


def _chat_repo_name(cloud) -> str:
    return (cloud.get("chat_repo") or f"{cloud['namespace']}/paper-chat-logs").split("/")[-1]


def create_chat_repo() -> bool:
    """在 ModelScope 建「私有」聊天数据集仓库（幂等，可重复调用）。

    用 SDK 建仓（visibility='private'，无默认 README/.gitattributes，空仓可直接 push）。
    SDK 不可用 / 建仓失败时返回 False，由调用方降级为只存本地。
    """
    got = _ctx()
    if not got:
        return False
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    repo = _chat_repo_name(cloud)
    repo_id = f"{cloud['namespace']}/{repo}"
    try:
        from modelscope.hub import HubApi
        HubApi().create_repo(repo_id=repo_id, token=token, repo_type="dataset",
                             visibility="private", exist_ok=True,
                             create_default_config=False)
        print(f"[cloud] 私有聊天仓库 {repo_id} 已就绪")
        return True
    except Exception as e:
        print(f"[cloud] 自动建聊天仓库失败（可手动在 ModelScope 建私有数据集仓库 {repo_id}）：{e}")
        return False


def push_chat_logs(message: str = "chore: 保存聊天记录") -> bool:
    """把 chat-logs/ 推送到私有 ModelScope 仓库；首次调用自动 init 本地 git 并建仓。
    缺 token 时返回 False（只存本地）。"""
    got = _ctx()
    if not got:
        return False
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    namespace = cloud["namespace"]
    repo = _chat_repo_name(cloud)
    chat_dir = ROOT / "chat-logs"
    chat_dir.mkdir(parents=True, exist_ok=True)
    if not (chat_dir / ".git").exists():
        _git(chat_dir, "init", "-q")
        _git(chat_dir, "config", "user.name", os.environ.get("GIT_USER_NAME", "you"))
        _git(chat_dir, "config", "user.email", os.environ.get("GIT_USER_EMAIL", "you@example.com"))
        create_chat_repo()
    return _sync_repo(chat_dir, namespace, repo, token, message)


def pull_chat_logs() -> bool:
    """从私有 ModelScope 拉取聊天记录到本地。"""
    got = _ctx()
    if not got:
        return False
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    namespace = cloud["namespace"]
    repo = _chat_repo_name(cloud)
    chat_dir = ROOT / "chat-logs"
    if not (chat_dir / ".git").exists():
        return False
    _ensure_remote(chat_dir, namespace, repo, token)
    p = _git(chat_dir, "pull", "origin", "master")
    return p.returncode == 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "sync":
        print("用法: python -m service.cloud [sync]")
        return
    sync()


if __name__ == "__main__":
    main()
