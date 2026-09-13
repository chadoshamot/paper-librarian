"""冒烟测试：验证 DeepSeek LLM 与 Zotero API 连通。"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

print("== 1. DeepSeek LLM ==")
from openai import OpenAI

c = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
r = c.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "只回复两个字：正常"}],
)
print("LLM 回复:", r.choices[0].message.content)

print("== 2. Zotero API ==")
from pyzotero import zotero

z = zotero.Zotero(os.environ["ZOTERO_USER_ID"], "user", os.environ["ZOTERO_API_KEY"])
print("Zotero 库条目数:", z.num_items())
print("Zotero last_modified_version:", z.last_modified_version())
