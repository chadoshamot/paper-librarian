"""LLM 客户端：OpenAI 兼容接口，可插拔（DeepSeek/Qwen/GLM/OpenAI/Ollama）。

改 key 只改 .env；改模型只改 config.yml 的 model 段；换 provider 改 base_url 即可。
"""
import os

from openai import OpenAI


class LLM:
    def __init__(self, model_cfg: dict):
        self.provider = model_cfg["provider"]
        self.api_key = os.environ.get(model_cfg["api_key_env"])
        if not self.api_key:
            raise RuntimeError(f"环境变量 {model_cfg['api_key_env']} 未设置，请检查 .env")
        self.client = OpenAI(api_key=self.api_key, base_url=model_cfg["base_url"])
        self.default = model_cfg.get("default")
        self.tasks = model_cfg.get("tasks", {})

    def chat(self, messages, task=None, temperature=0.2):
        model = self.tasks.get(task) or self.default
        resp = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return resp.choices[0].message.content
