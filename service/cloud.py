"""云端同步：python -m service.cloud [sync]

把 cache/ 的 PDF（git-lfs）与 knowledge-base/ 的 Markdown 增量推送到
ModelScope 两个数据集仓库（your-namespace/paper-library-pdfs + your-namespace/paper-knowledge-base）。
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
        sys.exit(1)
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    namespace = cloud["namespace"]
    _sync_repo(cfg.cache_dir, namespace, cloud["pdf_repo"].split("/")[-1], token,
               "chore: 同步论文 PDF（git-lfs）")
    _sync_repo(ROOT / "knowledge-base", namespace, cloud["kb_repo"].split("/")[-1], token,
               "chore: 同步双语知识库")


def push_kb(message: str = "chore: 保存联网对话") -> bool:
    """只推送 knowledge-base（含 chat-logs/），返回是否成功；缺 token 返回 False。"""
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
        sys.exit(1)
    cfg, token = got
    cloud = cfg.config["storage"]["cloud"]
    namespace = cloud["namespace"]
    for repo_dir, repo in [(cfg.cache_dir, cloud["pdf_repo"].split("/")[-1]),
                           (ROOT / "knowledge-base", cloud["kb_repo"].split("/")[-1])]:
        if not (repo_dir / ".git").exists():
            print(f"[cloud] {repo} 尚未初始化 git，跳过 pull")
            continue
        _ensure_remote(repo_dir, namespace, repo, token)
        p = _git(repo_dir, "pull", "origin", "master")
        line = (p.stdout or p.stderr or "").strip().replace("\n", " ")
        print(f"[cloud] {repo} pull: {line[-160:]}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "sync":
        print("用法: python -m service.cloud [sync]")
        return
    sync()


if __name__ == "__main__":
    main()
