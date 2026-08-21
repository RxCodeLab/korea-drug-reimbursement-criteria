"""공공데이터포털 의약품 제품 허가정보 수집기.

data/normalized/*.json의 항목 제목에서 검색어를 유도해 의약품 제품 허가정보
상세(효능효과 문서 포함)를 조회하고 data/mfds/items/<ITEM_SEQ>.json에 개정
이력을 병합한다. DATA_GO_KEY가 없으면 한 줄 안내 후 건너뛴다.
"""

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from common import DATA, http_get, redact_text

API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06"
DETAIL_URL = "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail"
NORMALIZED_DIR = DATA / "normalized"
ITEMS_DIR = DATA / "mfds" / "items"
SCHEMA_VERSION = 1
DEFAULT_PAGE_SIZE = 100
REQUEST_SLEEP = 0.3
RESULT_OK = "00"

CLASS_HEADER = re.compile(r"^\[[^\]]*\]\s*")
PUMMYEONG = re.compile(r"\(\s*품명\s*[:∶]?\s*([^)]*)\)")
ATTACHMENT_NAME = re.compile(r"\.(hwpx?|pdf|docx?|xlsx?|zip|txt)$", re.IGNORECASE)
TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")
LATIN = re.compile(r"[A-Za-z0-9]")
DATE_YYYYMMDD = re.compile(r"^\d{8}$")
FORM_SUFFIX = re.compile(
    r"\s+(?:경구제|주사제|외용제|흡입제|점안제|비강분무제|좌제|연고제|액제|패취제|서방형제제)$"
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_ee(raw: object) -> str:
    text = html.unescape(str(raw or ""))
    text = TAG.sub(" ", text)
    return WHITESPACE.sub(" ", text).strip()


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_titles(normalized_dir: Path) -> list[str]:
    titles: list[str] = []
    for path in sorted(normalized_dir.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for entry in document.get("entries") or []:
            title = entry.get("title") if isinstance(entry, dict) else None
            if title:
                titles.append(str(title))
    return titles


def term_groups_from_titles(titles: Iterable[str]) -> list[tuple[str, list[str]]]:
    """제목 → (성분 검색어, 품명 예시 fallback) 목록.

    '(품명' 앞머리를 성분 검색어로 쓰고, 첨부파일명과 한글 분류어(일반원칙 등
    라틴 문자가 없는 머리)는 버린다. 품명 괄호 안 예시는 쉼표/등 기준으로 쪼개
    item_name fallback 검색어로 쓴다. 대소문자 무시 중복을 제거한다.
    """
    groups: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for title in titles:
        head = CLASS_HEADER.sub("", title.split("(품명", 1)[0])
        head = WHITESPACE.sub(" ", head).strip()
        head = FORM_SUFFIX.sub("", head)
        if not head or ATTACHMENT_NAME.search(head) or not LATIN.search(head):
            continue
        head_key = head.casefold()
        if head_key in seen:
            continue
        seen.add(head_key)
        fallbacks: list[str] = []
        for inner in PUMMYEONG.findall(title):
            for part in re.split(r"[,등]", inner):
                part = WHITESPACE.sub(" ", part).strip()
                if not part:
                    continue
                part_key = part.casefold()
                if part_key in seen:
                    continue
                seen.add(part_key)
                fallbacks.append(part)
        groups.append((head, fallbacks))
    return groups


def redact_message(service_key: str, text: object) -> str:
    message = redact_text(str(text))
    forms = {service_key, urllib.parse.quote(service_key), urllib.parse.quote_plus(service_key)}
    for form in forms:
        if form:
            message = message.replace(form, "[REDACTED]")
    return message


def fetch_page(service_key: str, search_param: str, term: str, page_no: int, page_size: int) -> dict:
    params = {
        "serviceKey": service_key,
        "type": "json",
        "pageNo": str(page_no),
        "numOfRows": str(page_size),
        search_param: term,
    }
    raw = http_get(API_URL, params)
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"MFDS API 응답 파싱 실패: {redact_message(service_key, exc)}") from exc
    envelope = payload.get("response") if isinstance(payload, dict) else None
    header = (envelope or {}).get("header") or {}
    if header.get("resultCode") != RESULT_OK:
        raise RuntimeError(
            "MFDS API 오류: 코드={}, 메시지={}".format(
                redact_message(service_key, header.get("resultCode", "")),
                redact_message(service_key, header.get("resultMsg", "")),
            )
        )
    body = (envelope or {}).get("body")
    return body if isinstance(body, dict) else {}


def body_items(body: dict) -> list[dict]:
    items = body.get("items")
    if isinstance(items, dict) and "item" in items:
        items = items["item"]
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def collect_pages(
    service_key: str, search_param: str, term: str, page_size: int, max_items: int | None,
) -> list[dict]:
    """search_param=term 조건을 totalCount에 맞춰 페이지 단위로 수집한다."""
    collected: list[dict] = []
    page_no = 1
    while True:
        body = fetch_page(service_key, search_param, term, page_no, page_size)
        time.sleep(REQUEST_SLEEP)
        rows = body_items(body)
        collected.extend(rows)
        try:
            total = int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        if max_items is not None and len(collected) >= max_items:
            return collected[:max_items]
        if not rows or len(rows) < page_size or (total and len(collected) >= total):
            return collected
        page_no += 1


def collect_term(
    service_key: str, head: str, fallbacks: list[str], page_size: int, max_items: int | None,
) -> tuple[list[dict], str | None]:
    """main_item_ingr 우선 → 같은 검색어 item_name → 품명 item_name 순서로 시도한다."""
    attempts = [("main_item_ingr", head), ("item_name", head)]
    attempts += [("item_name", fallback) for fallback in fallbacks]
    for search_param, term in attempts:
        rows = collect_pages(service_key, search_param, term, page_size, max_items)
        if rows:
            return rows, search_param
    return [], None


def scalar_fields(item: dict, seq: str) -> dict:
    permit_date = str(item.get("ITEM_PERMIT_DATE") or "").strip()
    cancel_date = str(item.get("CANCEL_DATE") or "").strip()
    cancel_name = str(item.get("CANCEL_NAME") or "").strip()
    if not DATE_YYYYMMDD.match(permit_date):
        permit_date = ""
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": True,
        "item_seq": seq,
        "item_name": str(item.get("ITEM_NAME") or "").strip(),
        "entp_name": str(item.get("ENTP_NAME") or "").strip(),
        "permit_date": permit_date,
        "cancel_date": cancel_date,
        "status": cancel_name or ("취소" if cancel_date else "정상"),
        "main_item_ingr": str(item.get("MAIN_ITEM_INGR") or "").strip(),
        "edi_code": str(item.get("EDI_CODE") or "").strip(),
        "atc_code": str(item.get("ATC_CODE") or "").strip(),
        "source_url": f"{DETAIL_URL}?itemSeq={seq}",
    }


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".mfds-", suffix=".tmp", delete=False) as tmp:
        json.dump(value, tmp, ensure_ascii=False, indent=1)
        tmp.write("\n")
        temp_name = tmp.name
    os.replace(temp_name, path)


def merge_item(item: dict, items_dir: Path, observed_at: str) -> str:
    """항목 1건을 items_dir/<ITEM_SEQ>.json에 병합한다.

    최초 수집은 "new", 동일 내용 재수집은 last_observed_at만 갱신해
    "unchanged", 내용이 바뀌면 개정을 최신순 맨 앞에 추가해 "changed"를
    반환한다.
    """
    seq = str(item.get("ITEM_SEQ") or "").strip()
    if not seq:
        raise RuntimeError("ITEM_SEQ가 없는 항목은 저장할 수 없습니다")
    path = items_dir / f"{seq}.json"
    existing = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
    ee_text = normalize_ee(item.get("EE_DOC_DATA"))
    digest = content_sha256(ee_text)
    revision = {
        "revision_id": f"{seq}-{digest[:8]}",
        "content_sha256": digest,
        "ee_text": ee_text,
        "ee_doc_id": str(item.get("EE_DOC_ID") or "").strip(),
        "first_observed_at": observed_at,
        "last_observed_at": observed_at,
    }
    status = "new"
    revisions = [r for r in (existing or {}).get("revisions") or [] if isinstance(r, dict)]
    if revisions:
        if revisions[0].get("content_sha256") == digest:
            revision["first_observed_at"] = str(revisions[0].get("first_observed_at") or observed_at)
            revisions[0] = revision
            status = "unchanged"
        else:
            revisions.insert(0, revision)
            status = "changed"
    else:
        revisions = [revision]
    record = scalar_fields(item, seq)
    record["revisions"] = revisions
    atomic_json(path, record)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="공공데이터포털 의약품 제품 허가정보 수집")
    parser.add_argument("--max-terms", type=int, default=None, help="검색어 상한(기본: 전체)")
    parser.add_argument("--max-items", type=int, default=None, help="저장 항목 상한(기본: 제한 없음)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="페이지당 건수")
    args = parser.parse_args(argv)
    for name in ("max-terms", "max-items", "page-size"):
        value = getattr(args, name.replace("-", "_"))
        if value is not None and value < 1:
            parser.error(f"--{name}은(는) 1 이상이어야 합니다")

    service_key = os.environ.get("DATA_GO_KEY")
    if not service_key:
        print("DATA_GO_KEY 환경 변수가 설정되지 않아 의약품 허가정보 수집을 건너뜁니다.")
        return 0
    service_key = urllib.parse.unquote(service_key)

    groups = term_groups_from_titles(load_titles(NORMALIZED_DIR))
    if args.max_terms is not None:
        groups = groups[: args.max_terms]

    fetched = new_revisions = unchanged = 0
    seen: set[str] = set()
    for head, fallbacks in groups:
        if args.max_items is not None and fetched >= args.max_items:
            break
        budget = args.max_items - fetched if args.max_items is not None else None
        rows, _search_param = collect_term(service_key, head, fallbacks, args.page_size, budget)
        for row in rows:
            seq = str(row.get("ITEM_SEQ") or "").strip()
            if not seq or seq in seen:
                continue
            if args.max_items is not None and fetched >= args.max_items:
                break
            seen.add(seq)
            result = merge_item(row, ITEMS_DIR, now_utc())
            fetched += 1
            if result == "unchanged":
                unchanged += 1
            else:
                new_revisions += 1
    print(f"[MFDS] 수집 항목={fetched}건, 신규 개정={new_revisions}건, 변동 없음={unchanged}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
