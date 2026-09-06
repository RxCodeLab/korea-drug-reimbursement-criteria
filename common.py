import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "criteria.db"

OC = os.environ.get("LAW_OC")

RULE_NAME = "요양급여의 적용기준 및 방법에 관한 세부사항(약제)"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) drug-criteria-tracker/1.0"
DATE_YYYYMMDD = re.compile(r"^\d{8}$")
# URL 쿼리에 나타나면 인증정보로 간주하는 키. 저장·게시되는 모든 URL 검사가 이 집합 하나를 쓴다.
CREDENTIAL_QUERY_KEYS = frozenset({"oc", "law_oc", "api_key", "apikey", "key", "token", "access_token"})


def today_kst() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")


def parse_changes_since(value: str) -> str:
    """엄격한 YYYYMMDD 형식과 미래 날짜 여부를 검증한다."""
    if not DATE_YYYYMMDD.fullmatch(value):
        raise ValueError("--changes-since는 YYYYMMDD 형식이어야 합니다")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("--changes-since는 유효한 날짜여야 합니다") from exc
    if value > today_kst():
        raise ValueError("--changes-since는 오늘 이후일 수 없습니다")
    return value


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".json-", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(value, tmp, ensure_ascii=False, indent=1)
        tmp.write("\n")
        temp_name = tmp.name
    os.replace(temp_name, path)


def require_oc() -> str:
    if not OC:
        raise RuntimeError("LAW_OC 환경 변수를 설정해야 합니다")
    return OC


def has_credential_query(url: str) -> bool:
    """URL 쿼리에 인증정보 키가 들어 있으면 True. 해석할 수 없는 URL도 안전하지 않은 것으로 본다."""
    if "law_oc" in url.casefold():
        return True
    try:
        query = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return True
    return any(key.casefold() in CREDENTIAL_QUERY_KEYS for key, _ in query)


def credential_free_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    safe_query = [
        (key, value)
        for key, value in query
        if key.casefold() not in CREDENTIAL_QUERY_KEYS
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


# 소켓 단위 대기 시간. 해외 IP 차단으로 응답이 아예 없을 때 60초×3회가 호출마다 쌍여 30분을 낭비했다.
HTTP_TIMEOUT_SECONDS = 15


def http_get(url: str, params: dict | None = None, retries: int = 3) -> bytes:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.law.go.kr/"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET 요청 실패: {redact_url(url)}: {redact_text(str(last))}")


def api_json(target_url: str, params: dict) -> dict:
    raw = http_get(target_url, params)
    return json.loads(raw.decode("utf-8"))
