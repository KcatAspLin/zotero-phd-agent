"""Ollama adapter using the same classification schema and validation as OpenAI."""
import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .content import SYSTEM, classification_schema, validate_analysis
from .net import AgentError, HttpError


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    base_url: str = ""
    num_ctx: int = 32768

    @classmethod
    def from_env(cls):
        provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        if provider == "openai":
            return cls(provider, os.getenv("OPENAI_MODEL", "gpt-5-mini"))
        if provider != "ollama":
            raise AgentError("LLM_PROVIDER must be openai or ollama")
        model = os.getenv("OLLAMA_MODEL", "qwen3:8b").strip()
        if not model or len(model) > 200 or any(c.isspace() for c in model) or "cloud" in model.lower():
            raise AgentError("OLLAMA_MODEL must name a downloaded local model, for example qwen3:8b (cloud tags are not supported)")
        base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                     and parsed.port is not None and not parsed.username and not parsed.password
                     and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise AgentError("OLLAMA_BASE_URL must be a loopback URL such as http://127.0.0.1:11434")
        try:
            num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
        except ValueError as exc:
            raise AgentError("OLLAMA_NUM_CTX must be an integer") from exc
        if not 8192 <= num_ctx <= 131072:
            raise AgentError("OLLAMA_NUM_CTX must be between 8192 and 131072 and supported by your model")
        return cls(provider, model, base, num_ctx)

    def cache_settings(self):
        result = {"provider": self.provider, "model": self.model}
        if self.provider == "ollama":
            result.update(base_url=self.base_url, num_ctx=self.num_ctx)
        return result

    def matches(self, plan):
        if plan.get("provider", "openai") != self.provider or plan.get("model") != self.model:
            return False
        # Preserve older OpenAI caches; local model caches always record settings.
        return self.provider == "openai" or plan.get("analysis_config") == self.cache_settings()


def local_messages(meta, pdf):
    return [
        {"role": "system", "content": SYSTEM + "\nReturn only JSON matching this schema:\n" + json.dumps(classification_schema())},
        {"role": "user", "content": json.dumps({"bibliographic_metadata": meta,
          "sampled_pdf": pdf["sampled"], "paper_text": pdf["text"]}, ensure_ascii=False)},
    ]


def local_text_budget(meta, num_ctx):
    # Conservative UTF-8 byte bound for byte-based tokenizers such as Qwen3:
    # reserve 4096 output tokens and 2048 for chat-template overhead. Account
    # for JSON escaping too, rather than assuming every character is 1/4 token.
    empty = local_messages(meta, {"sampled": False, "text": ""})
    overhead = sum(len(m["content"].encode("utf-8")) for m in empty)
    budget = min(45000, (num_ctx - 4096 - 2048 - overhead) // 2)
    if budget < 1000:
        raise AgentError("Metadata leaves too little local model context; increase OLLAMA_NUM_CTX to a value supported by your model")
    return budget


def analyze_local(http, meta, pdf, config):
    messages = local_messages(meta, pdf)
    size = sum(len(m["content"].encode("utf-8")) for m in messages)
    if size > config.num_ctx - 4096 - 2048:
        raise AgentError("Local model input exceeds the conservative context budget; no Zotero changes made")
    try:
        response = http.request("POST", config.base_url + "/api/chat", allow_localhost=True, timeout=900,
                                json_data={"model": config.model, "messages": messages,
                                           "format": classification_schema(), "stream": False, "think": False,
                                           "options": {"temperature": 0, "num_ctx": config.num_ctx, "num_predict": 4096}}).json()
    except HttpError as exc:
        if exc.status == 404:
            raise AgentError(f"Local Ollama model not found. Download it with: ollama pull {config.model}") from exc
        raise AgentError(f"Ollama returned HTTP {exc.status}; check model availability, context size and free memory. No OpenAI fallback was used.") from exc
    except AgentError as exc:
        raise AgentError("Ollama request failed. Open the Ollama app (or run ollama serve), check free memory, then retry. No OpenAI fallback was used.") from exc
    if not isinstance(response, dict) or response.get("done") is not True or response.get("done_reason") not in (None, "stop"):
        raise AgentError("Local model response incomplete; no Zotero changes made")
    try:
        result = json.loads(response["message"]["content"])
        validate_analysis(result)
    except (ValueError, KeyError, TypeError) as exc:
        raise AgentError("Invalid local model JSON or tags; no Zotero changes made. Try a larger model or use --refresh to retry.") from exc
    return result
