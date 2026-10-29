import hashlib
import os
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

USER_AGENT = f"OAuth poe2planner/0.1 (contact: {os.environ.get('POE2PLANNER_CONTACT', 'https://github.com/Ericson3100/poe2-build-planner-agent')})"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

# llamadas reales por política (sin contar caché)
CALLS: dict[str, int] = {}
# cuándo se puede hacer la siguiente
_next_allowed: dict[str, float] = {}
# historial de llamadas por política
_history: dict[str, list[float]] = {}
MIN_INTERVAL = {"trade-search": 10.5, "trade-fetch": 1.5}


def _wait_seconds(headers, policy: str = "default") -> float:
    # segundos a esperar según los headers de rate limit de GGG
    wait = 0.0
    rules = headers.get("X-Rate-Limit-Rules")
    if not rules:
        return wait
    now = time.monotonic()
    for rule in rules.split(","):
        limits = headers.get(f"X-Rate-Limit-{rule}", "")
        states = headers.get(f"X-Rate-Limit-{rule}-State", "")
        for limit, state in zip(limits.split(","), states.split(",")):
            max_hits, period, _ = (int(x) for x in limit.split(":"))
            hits, _, penalty = (int(x) for x in state.split(":"))
            if penalty:
                wait = max(wait, penalty)
            elif hits >= max_hits - 1:
                mine = [t for t in _history.get(policy, []) if now - t < period]
                excess = hits - (max_hits - 2)
                wait = max(wait, mine[excess - 1] + period - now + 1 if len(mine) >= hits else period)
    return wait


def request_json(url: str, *, body: dict | None = None, ttl: float = 0, policy: str = "default"):
    cache_file = None
    if ttl > 0:
        key = hashlib.sha1((url + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        cache_file = CACHE_DIR / f"{key}.json"
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
            return json.loads(cache_file.read_text(encoding="utf-8"))

    delay = _next_allowed.get(policy, 0) - time.monotonic()
    if delay > 0:
        time.sleep(delay)

    headers = {"User-Agent": USER_AGENT}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    for attempt in range(2):
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=30) as resp:
                calls = _history.setdefault(policy, [])
                calls.append(time.monotonic())
                del calls[:-1000]
                _next_allowed[policy] = time.monotonic() + max(_wait_seconds(resp.headers, policy),
                                                               MIN_INTERVAL.get(policy, 0))
                payload = json.load(resp)
                CALLS[policy] = CALLS.get(policy, 0) + 1
                break
        except HTTPError as err:
            if err.code == 429 and attempt == 0:
                time.sleep(int(err.headers.get("Retry-After", "60")))
                continue
            raise

    if cache_file is not None:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
    return payload
