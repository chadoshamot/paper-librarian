"""Zotero Web API 封装。"""
from pyzotero import zotero


class ZoteroClient:
    def __init__(self, user_id, api_key):
        self.zot = zotero.Zotero(user_id, "user", api_key)

    def create_item(self, data: dict):
        """创建条目，返回 item key（失败返回 None）。"""
        resp = self.zot.create_items([data])
        return resp.get("successful", {}).get("0", {}).get("key")

    def attach_linked_file(self, parent_key: str, abs_path, title: str):
        """为条目添加「链接文件」附件（指向本地热缓存）。"""
        att = {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "linked_file",
            "title": title,
            "contentType": "application/pdf",
            "path": str(abs_path),
        }
        resp = self.zot.create_items([att])
        return resp.get("successful", {}).get("0", {}).get("key")

    def add_tags(self, item_key: str, tags):
        """给条目打标签（存分类字段），失败不抛异常。"""
        try:
            item = self.zot.item(item_key)
            self.zot.add_tags(item, *tags)
        except Exception as e:
            print(f"  [warn] 加标签失败: {e}")

    def delete_item(self, item_key: str):
        """删除条目（含附件子项）。失败不抛异常。"""
        try:
            for child in self.zot.children(item_key):
                self.zot.delete_item(self.zot.item(child["key"]))
            self.zot.delete_item(self.zot.item(item_key))
        except Exception as e:
            print(f"  [warn] 删除条目失败: {e}")

    def num_items(self):
        return self.zot.num_items()
