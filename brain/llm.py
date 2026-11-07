"""Minimal OpenAI-compatible chat client used by the optional LLM structuring path.

The MVP is fully usable without it. When ``BRAIN_LLM_API_KEY`` is set (and an
OpenAI-compatible base URL / model are configured), ingestion upgrades from the
deterministic heuristic extractor to an LLM-produced structured note.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


def extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
    raise LLMError("LLM response did not contain a JSON object")


class LLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LLMClient | None":
        env = os.environ if env is None else env
        api_key = env.get("BRAIN_LLM_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            base_url=env.get("BRAIN_LLM_BASE_URL", "https://api.openai.com/v1"),
            model=env.get("BRAIN_LLM_MODEL", "gpt-4o-mini"),
            timeout=float(env.get("BRAIN_LLM_TIMEOUT", "60")),
        )

    def chat_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            body = self._post(payload)
        except urllib.error.HTTPError as exc:
            # Some OpenAI-compatible gateways do not implement response_format.
            if exc.code not in (400, 404, 422):
                raise LLMError(f"LLM request failed: HTTP {exc.code}") from exc
            payload.pop("response_format", None)
            body = self._post(payload)
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc.reason}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response missing choices[0].message.content") from exc
        return extract_json_object(content)

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM response was not valid JSON") from exc
