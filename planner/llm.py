import os
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    if key in os.environ:
        return os.environ[key]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip()
    return default


CLAUDE_MODEL = "claude-opus-5-5"
PLANNER_MODEL = _env("CLAUDE_PLANNER_MODEL", CLAUDE_MODEL)
EXTRACT_MODEL = _env("CLAUDE_EXTRACT_MODEL", CLAUDE_MODEL)
FALLBACK_BETA = "server-side-fallback-2026-07-01"

PRICES = {
    # USD por millón de tokens: input, output, cache read, cache write
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    # Haiku 5.5: $0.10 / $0.50 hasta 100K tokens
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}

_clients: dict = {}


def short(model: str) -> str:
    return model.removeprefix("claude-")


def anthropic_client():
    if "anthropic" not in _clients:
        import anthropic
        key = _env("ANTHROPIC_API_KEY")
        if key:
            os.environ.setdefault("ANTHROPIC_API_KEY", key)
        # las keys sin workspace tienen que indicarlo en cada request
        workspace = _env("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        _clients["anthropic"] = anthropic.Anthropic(default_headers=headers)
    return _clients["anthropic"]


def _claude_params(effort: str, model: str | None = None) -> dict:
    return {"model": model or PLANNER_MODEL, "output_config": {"effort": effort},
            "betas": [FALLBACK_BETA], "fallbacks": "default"}


class UsageMeter:

    def __init__(self) -> None:
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.requests = 0
        self.usd_total = 0.0
        self.models: set[str] = set()

    def add(self, usage, model: str | None = None) -> None:
        if usage is None:
            return
        self.requests += 1
        t = {"input": usage.input_tokens or 0, "output": usage.output_tokens or 0,
             "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
             "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0}
        for k, v in t.items():
            self.tokens[k] += v
        model = model or PLANNER_MODEL
        self.models.add(model)
        price = PRICES.get(model, PRICES[CLAUDE_MODEL])
        self.usd_total += sum(t[k] * price[k] / 1e6 for k in t)

    @property
    def usd(self) -> float:
        return self.usd_total

    def add_to(self, totals: dict | None) -> dict:
        out = dict(totals or {"requests": 0, **{k: 0 for k in self.tokens}})
        out["requests"] += self.requests
        for k, v in self.tokens.items():
            out[k] += v
        return out

    def summary(self) -> str:
        t = self.tokens
        return (f"{'+'.join(sorted(self.models)) or 'claude'}: {self.requests} requests, in {t['input']:,}, "
                f"cache read {t['cache_read']:,}, cache write {t['cache_write']:,}, out {t['output']:,}, ${self.usd:.2f}")


class RefusalError(RuntimeError):
    pass


def _check_refusal(message) -> None:
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise RefusalError(f"Claude declined the request ({getattr(details, 'category', None)})")


def structured(system: str, user: str, schema: type[BaseModel], effort: str = "medium",
               meter: UsageMeter | None = None, model: str | None = None) -> BaseModel:
    meter = meter or UsageMeter()
    model = model or EXTRACT_MODEL
    response = anthropic_client().beta.messages.parse(
        **_claude_params(effort, model), max_tokens=16000, system=system,
        messages=[{"role": "user", "content": user}], output_format=schema)
    _check_refusal(response)
    meter.add(response.usage, model)
    return response.parsed_output
