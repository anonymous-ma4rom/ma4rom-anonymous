import json
import re
import time
from json import JSONDecoder

from openai import BadRequestError

from utils.llm_set import client
from config import (
    LLM_MODEL,
    LLM_FALLBACK_MODELS,
    LLM_TIMEOUT_SECONDS,
    LLM_MAX_RETRIES,
)
from utils.llm_metrics import (
    record_llm_attempt,
    record_llm_failure,
    record_llm_success,
)

_DEFAULT_SYSTEM = (
    "你是本体对齐专家（OBDA）。"
    "从给定候选列表中选出最符合的一项。"
    "严格输出 JSON，不含任何 Markdown 包裹或注释。"
)


def call_llm(prompt: str, system: str = None, prefer_fast: bool = True) -> dict:
    """
    调用 LLM，返回解析后的 dict。
    模型名称从 config.LLM_MODEL 读取。
    """
    def _extract_json_dict(raw_text: str) -> dict:
        text = (raw_text or "").strip()
        # 去掉 markdown 包裹 / 思维链标签
        text = re.sub(r"^```json|^```|```$", "", text, flags=re.MULTILINE).strip()
        text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()

        decoder = JSONDecoder()
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        raise json.JSONDecodeError("No valid JSON object found in model output", text, 0)

    fallback_models = [m for m in (LLM_FALLBACK_MODELS or []) if m != LLM_MODEL]
    if prefer_fast and fallback_models:
        models = [fallback_models[0], LLM_MODEL] + [m for m in fallback_models[1:] if m != fallback_models[0]]
    else:
        models = [LLM_MODEL] + fallback_models
    last_err = None

    for model in models:
        for attempt in range(1, max(1, int(LLM_MAX_RETRIES)) + 1):
            try:
                system_prompt = system or _DEFAULT_SYSTEM
                record_llm_attempt()
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                record_llm_success(
                    response,
                    model=model,
                    prompt=prompt,
                    system=system_prompt,
                )
                raw = (response.choices[0].message.content or "").strip()
                return _extract_json_dict(raw)
            except BadRequestError as e:
                record_llm_failure()
                last_err = e
                msg = str(e).lower()
                # 模型不存在时，直接切到下一候选模型
                if "model not exist" in msg or "invalid_request_error" in msg:
                    break
                if attempt >= max(1, int(LLM_MAX_RETRIES)):
                    break
                time.sleep(min(2 ** (attempt - 1), 4))
            except Exception as e:
                record_llm_failure()
                last_err = e
                if attempt >= max(1, int(LLM_MAX_RETRIES)):
                    break
                time.sleep(min(2 ** (attempt - 1), 4))

    if last_err:
        raise last_err
    raise RuntimeError("LLM call failed with unknown error")
