#!/usr/bin/env python3
"""AI 论文管家 · 一键安装脚本（交互式，纯标准库，装依赖前即可运行）。

用法：git clone 本仓库后，在仓库根目录运行
    python setup.py

脚本会依次：检查 Python → 建目录骨架 → 导入/初始化论文库 → 生成配置与 .env
→ 安装依赖 → 冒烟验证。密钥只写进 .env（已 gitignore，不会上传）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO_NS = "shamot"  # 公开示例库所在的 ModelScope 命名空间（只读导入）


def ask(prompt: str, default: str = "") -> str:
    v = input(prompt).strip()
    return v if v else default


def ask_bool(prompt: str, default: str) -> bool:
    v = input(prompt).strip().lower()
    if not v:
        v = default
    return v in ("y", "yes", "1", "true")


def section(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")


def check_python() -> None:
    vi = sys.version_info
    print(f"  当前 Python {vi.major}.{vi.minor}.{vi.micro}  ->  {sys.executable}")
    if vi < (3, 10):
        print("  [错误] 需要 Python 3.10+，请升级后重试。")
        sys.exit(1)
    if vi >= (3, 14):
        print("  [提示] Python 3.14 较新，onnxruntime/modelscope 的 wheel 可能滞后；")
        print("         若下面装依赖报错，建议改用 Python 3.11 / 3.12。")
    print("  [提示] 若装了 conda，请用目标环境运行本脚本（脚本用同一个 python 装依赖）。")


def make_dirs() -> None:
    for d in ("cache", "papers", "index", "reports/daily", "models"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    print("  已就绪：cache/ papers/ index/ reports/daily/ models/")


def git_clone(url: str, dest: Path) -> bool:
    """git clone 公开仓库到 dest（成功后剥掉 .git，作为纯数据目录）。"""
    if dest.exists() and any(dest.iterdir()):
        print(f"  {dest.name}/ 非空，跳过（保留现有数据）")
        return False
    print(f"  正在 git clone {url} ...")
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                           capture_output=True, text=True)
    except FileNotFoundError:
        print(f"  [警告] 未找到 git，无法自动导入 {dest.name}；可手动下载或选自建库。")
        return False
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        print(f"  [警告] 导入 {dest.name} 失败：{tail[-1] if tail else '未知错误'}")
        return False
    shutil.rmtree(dest / ".git", ignore_errors=True)
    print(f"  已导入 {dest.name}/")
    return True


def gen_config(namespace: str) -> None:
    tpl = (ROOT / "config.example.yml").read_text(encoding="utf-8")
    (ROOT / "knowledge-base" / "config.yml").write_text(
        tpl.replace("__NAMESPACE__", namespace), encoding="utf-8")
    shutil.copyfile(ROOT / "taxonomy.yml", ROOT / "knowledge-base" / "taxonomy.yml")
    print(f"  已生成 knowledge-base/config.yml（namespace={namespace}）与 taxonomy.yml")


def write_env(env: dict) -> None:
    lines = [
        "# ── 密钥文件：由 setup.py 生成，已被 .gitignore 排除，绝不上传 ──",
        "",
        "# LLM（DeepSeek，论文分类/摘要/每日裁判）",
        f"DEEPSEEK_API_KEY={env.get('DEEPSEEK_API_KEY', '')}",
        "",
        "# ModelScope（云端同步 PDF/KB）",
        f"MODELSCOPE_TOKEN={env.get('MODELSCOPE_TOKEN', '')}",
        "",
        "# Kimi 联网对话（可选）",
        f"MOONSHOT_API_KEY={env.get('MOONSHOT_API_KEY', '')}",
        "",
        "# Zotero（可选，链接文件模式）",
        f"ZOTERO_USER_ID={env.get('ZOTERO_USER_ID', '')}",
        f"ZOTERO_API_KEY={env.get('ZOTERO_API_KEY', '')}",
        "",
        "# 邮箱推送（可选，QQ 邮箱；SMTP_PASS 是授权码，不是登录密码）",
        f"SMTP_HOST={env.get('SMTP_HOST', 'smtp.qq.com')}",
        f"SMTP_PORT={env.get('SMTP_PORT', '465')}",
        f"SMTP_USER={env.get('SMTP_USER', '')}",
        f"SMTP_PASS={env.get('SMTP_PASS', '')}",
        f"SMTP_TO={env.get('SMTP_TO', '')}",
        "",
    ]
    (ROOT / ".env").write_text("\n".join(lines), encoding="utf-8")
    print("  已生成 .env")


def install_deps() -> None:
    if not ask_bool("  现在安装依赖？[Y/n]", "y"):
        print("  跳过。稍后可手动：python -m pip install -r requirements.txt")
        return
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        str(ROOT / "requirements.txt")])
    if r.returncode != 0:
        print("  [警告] 依赖安装可能未成功，请检查上面的报错。")


def smoke() -> None:
    print("  （只做关键词检索，不触发嵌入模型下载）")
    code = (
        "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
        + "from service.config_loader import Config\n"
        + "from service.retrieval import load_documents, keyword_search\n"
        + "cfg = Config()\n"
        + "docs = load_documents(cfg)\n"
        + "print('配置加载成功，论文库', len(docs), '篇')\n"
        + "print('关键词检索命中', len(keyword_search(docs, 'scheduling', 3)), '篇')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("  " + "\n  ".join(r.stdout.strip().splitlines()))
    else:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        print("  [警告] 冒烟未通过（依赖可能没装全）：" + (tail[-1] if tail else ""))


def finish(mode: str) -> None:
    print("\n" + "=" * 60)
    print("  安装完成！启动方式：")
    print("=" * 60)
    print("    python -m service            # 交互式 CLI（主菜单）")
    print("    python -m service.web        # 网页界面 http://127.0.0.1:8000")
    print('    python -m service.search "GPU 集群调度"   # 命令行检索')
    if mode == "A":
        print("\n  当前为【示例库只读】模式：可检索/浏览示例论文。")
        print("  若要自建库上云：编辑 knowledge-base/config.yml 的 storage.cloud 填你的")
        print("  namespace，并在 .env 填 MODELSCOPE_TOKEN，然后重跑 python setup.py 选 B。")


def main() -> None:
    print("=" * 60)
    print("  AI 论文管家 · 一键安装")
    print("=" * 60)

    section(1, 7, "检查 Python 版本")
    check_python()

    section(2, 7, "建目录骨架")
    make_dirs()

    section(3, 7, "数据引导")
    print("  A. 导入公开示例论文库（18 篇，只读浏览/检索，零 token）")
    print("  B. 自建库（填你自己的 ModelScope，可完整上云/推库）")
    mode = "B" if ask_bool("  选 B 自建库吗？[y/N]", "n") else "A"
    if mode == "A":
        git_clone(f"https://www.modelscope.cn/datasets/{DEMO_NS}/paper-knowledge-base.git",
                  ROOT / "knowledge-base")
        git_clone(f"https://www.modelscope.cn/datasets/{DEMO_NS}/paper-library-pdfs.git",
                  ROOT / "cache")
        (ROOT / "knowledge-base" / "fields").mkdir(parents=True, exist_ok=True)
        namespace = DEMO_NS
    else:
        (ROOT / "knowledge-base" / "fields").mkdir(parents=True, exist_ok=True)
        namespace = ask("  你的 ModelScope 用户名（namespace）：")
        if not namespace:
            print("  [提示] 未填 namespace，先用 your-namespace 占位（上云功能暂不可用）。")
            namespace = "your-namespace"

    section(4, 7, "生成配置")
    gen_config(namespace)

    section(5, 7, "填写密钥（可选项直接回车跳过）")
    env = {}
    if (ROOT / ".env").exists():
        if not ask_bool("  检测到已有 .env，覆盖？[y/N]", "n"):
            print("  保留现有 .env。")
            env = None
    if env is not None:
        env["DEEPSEEK_API_KEY"] = ask("  DeepSeek API Key（分类/摘要/每日裁判，platform.deepseek.com）：")
        env["MODELSCOPE_TOKEN"] = (ask("  ModelScope 令牌（modelscope.cn → 个人中心 → 访问令牌）：")
                                   if mode == "B" else "")
        env["MOONSHOT_API_KEY"] = ask("  Kimi API Key（联网对话，可选，platform.moonshot.cn）：")
        env["ZOTERO_USER_ID"] = ask("  Zotero 用户 ID（可选，zotero.org/settings/keys）：")
        env["ZOTERO_API_KEY"] = ask("  Zotero API Key（可选）：")
        env["SMTP_USER"] = ask("  QQ 邮箱账号（可选，邮件推送）：")
        env["SMTP_PASS"] = ask("  QQ 邮箱授权码（可选）：")
        env["SMTP_TO"] = ask("  收件人邮箱（可选，默认同发件人）：") or env["SMTP_USER"]
        env["SMTP_HOST"] = "smtp.qq.com"
        env["SMTP_PORT"] = "465"
        write_env(env)

    section(6, 7, "安装依赖")
    install_deps()

    section(7, 7, "冒烟验证")
    smoke()

    finish(mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消。可随时重跑 python setup.py 继续。")
