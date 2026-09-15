"""一次性校准：用 README 权威标题/描述修正 KB 与 Zotero 的论文认知（保证认知准确性）。
运行：python -m service.reconcile_truth
"""
import re
from pathlib import Path

from pyzotero import zotero as _zot

from .config_loader import Config
from .manifest import Manifest

# pid -> 修正字段。含 core_zh 则整段重写；否则仅改 title/venue。
# 这是针对「你自己论文库」的一次性校准表，示例格式如下（按需填充你的 pid）：
#
# CORRECTIONS = {
#     "local:Example_Paper_MLSys2024": {
#         "title_en": "Example: A Corrected English Title",
#         "venue": "MLSys",
#     },
#     "2401.12345": {  # 按 arXiv 编号
#         "title_en": "Another Corrected Title",
#         "title_zh": "中文标题",
#         "core_zh": "中文核心内容摘要……",
#         "core_en": "English core idea summary...",
#         "sig_zh": "中文意义……",
#         "sig_en": "English significance...",
#     },
# }
CORRECTIONS = {}


def main():
    cfg = Config()
    root = cfg.root
    man = Manifest(root / "knowledge-base" / "_manifest.json")
    z = _zot.Zotero(cfg.zotero_user_id, "user", cfg.zotero_api_key)

    for pid, c in CORRECTIONS.items():
        entry = man.get(pid)
        if not entry:
            print(f"[skip] manifest 无 {pid}")
            continue
        area_zh = (entry.get("area") or "").split("::")[-1]
        work = (entry.get("work_slugs") or ["uncategorized"])[0]
        kb_path = root / "knowledge-base" / "fields" / area_zh / work / f"{Path(entry['path']).stem}.md"

        # 1. Zotero 标题
        if c.get("title_en") and entry.get("zotero_key"):
            try:
                it = z.item(entry["zotero_key"])
                it["data"]["title"] = c["title_en"]
                z.update_item(it)
            except Exception as e:
                print(f"[warn] Zotero 标题 {pid}: {e}")

        # 2. KB 文件
        head, fm, tail = kb_path.read_text(encoding="utf-8").split("---", 2)
        if c.get("title_en"):
            fm = re.sub(r'title_en: ".*"', f'title_en: "{c["title_en"]}"', fm)
        if c.get("title_zh"):
            fm = re.sub(r'title_zh: ".*"', f'title_zh: "{c["title_zh"]}"', fm)
        if c.get("venue"):
            fm = re.sub(r"venue: .*", f"venue: {c['venue']}", fm)
        if "core_zh" in c:
            body = (f"## 核心内容（中文）\n{c['core_zh']}\n\n"
                    f"## Core idea (English)\n{c['core_en']}\n\n"
                    f"## 意义（中文）\n{c['sig_zh']}\n\n"
                    f"## Significance (English)\n{c['sig_en']}\n")
            kb_path.write_text("---" + fm + "---\n\n" + body, encoding="utf-8")
        else:
            kb_path.write_text("---" + fm + "---" + tail, encoding="utf-8")

        # 3. manifest 标题
        if c.get("title_en"):
            entry["title"] = c["title_en"]

    man.save()
    print("reconcile done")


if __name__ == "__main__":
    main()
