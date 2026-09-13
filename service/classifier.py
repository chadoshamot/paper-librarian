"""LLM 分类：类别 / 大领域 / 主要工作 / 年份。"""
import json

from .llm import LLM


class Classifier:
    def __init__(self, llm: LLM, taxonomy: dict):
        self.llm = llm
        self.taxonomy = taxonomy

    def classify(self, title: str, abstract: str = "") -> dict:
        categories = self.taxonomy["category"]["values"]
        fields = []
        for group, areas in self.taxonomy["broad_field"]["groups"].items():
            for a in areas:
                fields.append(f"{group}::{a}")
        known_slugs = list(self.taxonomy["work_slug"]["entries"].keys())

        prompt = f"""你是论文分类器。给下面这篇论文打标签，只输出一个 JSON 对象，不要任何其它文字。

论文标题：{title}
摘要：{abstract[:2000] or "（无摘要，请基于你对论文的了解）"}

可选「类别」：{categories}
可选「大领域」（格式：顶层学科::具体方向）：{fields}
已有「主要工作」slug：{known_slugs}（可提新 slug，但须小写英文连字符；粒度判断线：同 slug 的论文解决同一子问题/用同一类方法）

严格输出如下结构的 JSON：
{{"category": "...", "area": "顶层::具体", "work_slugs": ["最核心slug", "次要slug"], "year": 2023}}

说明：work_slugs 按主次排序，第 1 个是最能代表本文子问题/方法的 slug（用于命名与目录）；year 是论文发表年份，无法从标题/摘要判断则填 null，不要臆造当前年份。"""
        raw = self.llm.chat([{"role": "user", "content": prompt}], task="classify")
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> dict:
        raw = raw.strip().strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        return json.loads(raw.strip())
