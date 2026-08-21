import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "criteria.db"

OC = os.environ.get("LAW_OC")

RULE_NAME = "요양급여의 적용기준 및 방법에 관한 세부사항(약제)"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) drug-criteria-tracker/1.0"


def require_oc() -> str:
    if not OC:
        raise RuntimeError("LAW_OC 환경 변수를 설정해야 합니다")
    return OC


def credential_free_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    safe_query = [
        (key, value)
        for key, value in query
        if key.lower() not in {"oc", "api_key", "apikey", "key", "token", "access_token"}
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(safe_query), "")
    )


def redact_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return re.sub(
            r"https?://[^\s'\"<>]+",
            lambda match: redact_url(match.group(0)),
            url,
        )
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    keys = [key for key, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    query = urllib.parse.urlencode([(key, "[REDACTED]") for key in keys])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def redact_text(text: str) -> str:
    text = redact_url(text)
    return re.sub(
        r"(?i)\b(oc|api_key|apikey|key|token|access_token)=([^&\s'\"<>]+)",
        r"\1=[REDACTED]",
        text,
    )


def http_get(url: str, params: dict | None = None, retries: int = 3) -> bytes:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.law.go.kr/"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET 요청 실패: {redact_url(url)}: {redact_text(str(last))}")


def api_json(target_url: str, params: dict) -> dict:
    raw = http_get(target_url, params)
    return json.loads(raw.decode("utf-8"))
