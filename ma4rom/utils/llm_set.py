from openai import OpenAI
from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_TIMEOUT_SECONDS,
    LLM_MAX_RETRIES,
)   # 统一从 config.py 读取

client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
    timeout=LLM_TIMEOUT_SECONDS,
    max_retries=max(0, int(LLM_MAX_RETRIES) - 1),
)
