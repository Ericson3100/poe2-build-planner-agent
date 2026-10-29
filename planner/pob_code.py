import base64
import re
import zlib
from urllib.request import Request, urlopen

from planner.net import USER_AGENT

_POBBIN_RE = re.compile(r"pobb\.in/(?:u/[^/]+/)?([A-Za-z0-9_-]+)")


def decode_code(code: str) -> str:
    code = code.strip().replace("+", "-").replace("/", "_")
    padded = code + "=" * (-len(code) % 4)
    return zlib.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8")


def resolve(code_or_link: str) -> str:
    text = code_or_link.strip()
    match = _POBBIN_RE.search(text)
    if match:
        req = Request(f"https://pobb.in/{match.group(1)}/raw", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    return decode_code(text)
