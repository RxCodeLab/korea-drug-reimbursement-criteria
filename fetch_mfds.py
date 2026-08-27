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
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import DATA, http_get, redact_text

API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06"
DETAIL_URL = "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail"
HISTORY_URL = "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemChangeHistList"
NORMALIZED_DIR = DATA / "normalized"
ITEMS_DIR = DATA / "mfds" / "items"
SCHEMA_VERSION = 1
DEFAULT_PAGE_SIZE = 100
# 요청 간 대기 없음. 차단이 잦아지면 이 값을 올려 완화한다(예: 0.3).
REQUEST_SLEEP = 0.0
RESULT_OK = "00"
HISTORY_FAILURE_LIMIT = 5
TERM_FAILURE_LIMIT = 10

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
INGR_CODE = re.compile(r"^\[[^\]]*\]\s*")
HISTORY_ENTRY = re.compile(
    r'data-docdata="(.*?)"\s+onclick="detailHist\(&#39;([^&]+)&#39;, &#39;(\d{4}-\d{2}-\d{2})&#39;',
    re.DOTALL,
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_ee(raw: object) -> str:
    text = html.unescape(str(raw or ""))
    if text.lstrip().startswith("<?xml") or text.lstrip().startswith("<DOC"):
        try:
            text = " ".join(ET.fromstring(text).itertext())
        except ET.ParseError:
            pass
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


def fetch_page(service_key: str, query: dict[str, str], page_no: int, page_size: int) -> dict:
    params = {
        "serviceKey": service_key,
        "type": "json",
        "pageNo": str(page_no),
        "numOfRows": str(page_size),
        **query,
    }
    raw = http_get(API_URL, params)
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"MFDS API 응답 파싱 실패: {redact_message(service_key, exc)}") from exc
    envelope = payload if isinstance(payload, dict) else {}
    if isinstance(envelope.get("response"), dict):
        envelope = envelope["response"]
    header = envelope.get("header") or {}
    if header.get("resultCode") != RESULT_OK:
        raise RuntimeError(
            "MFDS API 오류: 코드={}, 메시지={}".format(
                redact_message(service_key, header.get("resultCode", "")),
                redact_message(service_key, header.get("resultMsg", "")),
            )
        )
    body = envelope.get("body")
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


def parse_history(raw: bytes, item_seq: str, observed_at: str) -> list[dict]:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"MFDS 허가이력 응답 파싱 실패: {exc}") from exc
    revisions: list[dict] = []
    seen: set[str] = set()
    for document, official_id, official_date in HISTORY_ENTRY.findall(source):
        text = normalize_ee(html.unescape(document))
        digest = content_sha256(text)
        if digest in seen:
            continue
        seen.add(digest)
        revisions.append({
            "revision_id": f"{item_seq}-{digest[:8]}",
            "content_sha256": digest,
            "ee_text": text,
            "ee_doc_id": official_id,
            "official_revision_date": official_date,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
        })
    return revisions


def fetch_history(item_seq: str, observed_at: str) -> list[dict]:
    raw = http_get(HISTORY_URL, {"itemSeq": item_seq, "docType": "EE", "page": "1"})
    time.sleep(REQUEST_SLEEP)
    return parse_history(raw, item_seq, observed_at)


def collect_query(
    service_key: str, query: dict[str, str], page_size: int, max_items: int | None,
) -> list[dict]:
    """질의 조건을 totalCount에 맞춰 페이지 단위로 수집한다."""
    collected: list[dict] = []
    page_no = 1
    while True:
        body = fetch_page(service_key, query, page_no, page_size)
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


def collect_pages(
    service_key: str, search_param: str, term: str, page_size: int, max_items: int | None,
) -> list[dict]:
    """search_param=term 조건을 totalCount에 맞춰 페이지 단위로 수집한다."""
    return collect_query(service_key, {search_param: term}, page_size, max_items)


def collect_changed(
    service_key: str, start_date: str, end_date: str, page_size: int,
) -> list[dict]:
    """변경일자 구간의 품목 변경분을 받아온다.

    공공데이터포털은 증분 인자 없는 반복 호출을 시간당 100회로 제한하므로,
    평시 실행은 전체 검색 대신 이 변경분 질의를 쓴다.
    """
    return collect_query(
        service_key,
        {"start_change_date": start_date, "end_change_date": end_date},
        page_size, None,
    )


# 한글 성분명 끝의 염·수화물·에스터류 접미어. 기본 성분명을 얻을 때 반복 제거한다.
SALT_SUFFIXES = (
    "수화물", "무수물", "반수화물", "일수화물", "이수화물", "삼수화물",
    "프로판디올", "포르메이트", "베실산염", "캄실산염", "토실산염", "메실산염",
    "푸마르산염", "타르타르산염", "말레산염", "옥살산염", "숙신산염", "아세트산염",
    "시트르산염", "시트르산", "염산염", "브롬화수소산염", "황산염", "인산염", "질산염",
    "나트륨", "칼륨", "칼슘", "마그네슘",
)
TRAILING_PAREN = re.compile(r"\([^)]*\)$")


def base_ingredient(name: str) -> str:
    """염·수화물 접미어를 벗겨 기본 성분명을 얻는다. 예) 다파글리플로진포르메이트 → 다파글리플로진"""
    name = TRAILING_PAREN.sub("", name.strip())
    changed = True
    while changed and len(name) > 3:
        changed = False
        for suffix in SALT_SUFFIXES:
            if name.endswith(suffix) and len(name) - len(suffix) > 2:
                name = name[: -len(suffix)].strip()
                changed = True
    return name


def base_ingredient_set(row: dict) -> frozenset[str]:
    """품목의 MAIN_ITEM_INGR을 기본 성분명 집합으로 정규화한다."""
    return frozenset(
        base for part in str(row.get("MAIN_ITEM_INGR") or "").split("|")
        if (base := base_ingredient(INGR_CODE.sub("", part.strip())))
    )


def expand_by_ingredient(
    service_key: str, rows: list[dict], page_size: int, max_items: int | None,
) -> list[dict]:
    """품명으로 찾은 품목의 한글 성분명으로 재검색해 다른 염의 제네릭까지 넓힌다.

    성분 검색은 한글명만 매칭되고 고시 제목의 성분명은 영문이라, 품명 검색이
    성공하면 그 품목의 기본 성분명(염·수화물 접미어 제거)으로 확장한다. 성분
    조합마다 가장 긴(가장 특이적인) 성분 하나만 조회해 메트포르민 같은 범용
    성분 전체를 쓸어오지 않게 하고, 시드 조합을 포함하는 품목만 채택한다.
    """
    merged = {str(row.get("ITEM_SEQ") or ""): row for row in rows}
    for seed in {base_ingredient_set(row) for row in rows} - {frozenset()}:
        probe = max(seed, key=len)
        for row in collect_pages(service_key, "main_item_ingr", probe, page_size, max_items):
            if seed <= base_ingredient_set(row):
                merged.setdefault(str(row.get("ITEM_SEQ") or ""), row)
    expanded = list(merged.values())
    return expanded[:max_items] if max_items is not None else expanded


def collect_term(
    service_key: str, head: str, fallbacks: list[str], page_size: int, max_items: int | None,
) -> tuple[list[dict], str | None]:
    """main_item_ingr 우선 → 같은 검색어 item_name → 품명 item_name 순서로 시도한다."""
    attempts = [("main_item_ingr", head), ("item_name", head)]
    attempts += [("item_name", fallback) for fallback in fallbacks]
    for search_param, term in attempts:
        rows = collect_pages(service_key, search_param, term, page_size, max_items)
        if rows:
            if search_param == "item_name":
                rows = expand_by_ingredient(service_key, rows, page_size, max_items)
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
        "main_item_ingr_eng": str(item.get("MAIN_INGR_ENG") or "").strip(),
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
    history_fetched_at = str((existing or {}).get("history_fetched_at") or "").strip()
    if history_fetched_at:
        record["history_fetched_at"] = history_fetched_at
    record["revisions"] = revisions
    atomic_json(path, record)
    return status


def merge_history(
    item_seq: str, history: list[dict], items_dir: Path, observed_at: str | None = None,
) -> int:
    """허가이력을 병합한다.

    API가 현재로 보고한 개정에는 official_revision_date가 없어 날짜 역순 정렬에서
    과거 개정에 밀리므로, 정렬 뒤에도 맨 앞에 오도록 고정한다. 수집 시각을
    history_fetched_at에 남겨 다음 실행이 재수집 대상을 판단하게 한다.
    """
    path = items_dir / f"{item_seq}.json"
    item = json.loads(path.read_text(encoding="utf-8"))
    revisions = item["revisions"]
    current_hash = revisions[0]["content_sha256"] if revisions else ""
    by_hash = {revision["content_sha256"]: revision for revision in revisions}
    added = 0
    for official in history:
        existing = by_hash.get(official["content_sha256"])
        if existing is not None:
            existing["ee_doc_id"] = official["ee_doc_id"]
            existing["official_revision_date"] = official["official_revision_date"]
            continue
        revisions.append(official)
        by_hash[official["content_sha256"]] = official
        added += 1
    revisions.sort(
        key=lambda revision: (
            revision["content_sha256"] == current_hash,
            revision.get("official_revision_date", ""),
            revision["first_observed_at"],
        ),
        reverse=True,
    )
    item["history_fetched_at"] = observed_at or now_utc()
    atomic_json(path, item)
    return added


def history_pending(item_seq: str, items_dir: Path) -> bool:
    """허가이력을 아직 한 번도 수집하지 않은 품목이면 True를 반환한다."""
    try:
        item = json.loads((items_dir / f"{item_seq}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not str(item.get("history_fetched_at") or "").strip()


SYNC_PATH = DATA / "mfds" / "sync.json"


def load_sync() -> dict | None:
    """마지막 변경분 동기화 상태를 읽는다. 없거나 손상됐으면 None."""
    try:
        sync = json.loads(SYNC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(sync, dict) or not str(sync.get("last_change_date") or "").strip():
        return None
    return sync


def save_sync(last_change_date: str, seen_heads: set[str]) -> None:
    atomic_json(SYNC_PATH, {
        "last_change_date": last_change_date,
        "seen_heads": sorted(seen_heads),
    })


def today_kst() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")


def stored_ingredients(items_dir: Path) -> frozenset[str]:
    """저장된 품목 전체의 기본 성분명 집합. 변경분에서 유관 신규 품목을 고르는 기준."""
    bases: set[str] = set()
    for path in items_dir.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bases |= base_ingredient_set({"MAIN_ITEM_INGR": item.get("main_item_ingr", "")})
    return frozenset(bases)


def _print_summary(stats: dict) -> None:
    print(
        f"[MFDS] 수집 항목={stats['fetched']}건, 신규 개정={stats['new']}건, "
        f"과거 허가이력={stats['history']}건, 변동 없음={stats['unchanged']}건, "
        f"이력 미수집={stats['history_skipped']}건, 검색 실패={stats['term_failures']}건"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="공공데이터포털 의약품 제품 허가정보 수집")
    parser.add_argument("--max-terms", type=int, default=None, help="검색어 상한(기본: 전체)")
    parser.add_argument("--max-items", type=int, default=None, help="저장 항목 상한(기본: 제한 없음)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="페이지당 건수")
    parser.add_argument("--skip-history", action="store_true", help="의약품안전나라 효능·효과 변경이력 수집 생략")
    parser.add_argument("--full", action="store_true",
                        help="변경분 대신 전체 검색어로 수집(최초 구축·재구축용)")
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

    stats = {"fetched": 0, "new": 0, "history": 0, "unchanged": 0,
             "history_skipped": 0, "term_failures": 0}
    history_state = {"enabled": not args.skip_history, "failures": 0}
    seen: set[str] = set()

    def budget_left() -> int | None:
        return None if args.max_items is None else args.max_items - stats["fetched"]

    def process_row(row: dict) -> None:
        seq = str(row.get("ITEM_SEQ") or "").strip()
        if not seq or seq in seen:
            return
        seen.add(seq)
        observed_at = now_utc()
        result = merge_item(row, ITEMS_DIR, observed_at)
        stats["fetched"] += 1
        if result == "unchanged":
            stats["unchanged"] += 1
        else:
            stats["new"] += 1
        if history_state["enabled"] and (
            result != "unchanged" or history_pending(seq, ITEMS_DIR)
        ):
            # 허가이력 실패는 품목 단위로 넘긴다. 마커가 남지 않으므로
            # 해당 품목은 다음 실행에서 백필되고, 연속 실패가 이어지면
            # 이번 실행의 이력 수집만 중단해 품목 수집을 지킨다.
            try:
                stats["history"] += merge_history(
                    seq, fetch_history(seq, observed_at), ITEMS_DIR, observed_at,
                )
                history_state["failures"] = 0
            except RuntimeError as exc:
                history_state["failures"] += 1
                stats["history_skipped"] += 1
                print(f"허가이력 수집 실패({seq}): {exc}")
                if history_state["failures"] >= HISTORY_FAILURE_LIMIT:
                    history_state["enabled"] = False
                    print("연속 실패로 이번 실행의 허가이력 수집을 중단합니다. "
                          "미수집 품목은 다음 실행에서 백필합니다.")

    def sweep(sweep_groups: list[tuple[str, list[str]]]) -> set[str]:
        """검색어 목록을 훑어 수집하고, 성공한 검색어 머리를 돌려준다.

        검색 실패는 검색어 단위로 넘긴다. 실패한 검색어는 다음 실행에서
        다시 시도되고, 연속 실패가 이어지면 지금까지 저장분을 지키며 멈춘다.
        """
        succeeded: set[str] = set()
        consecutive = 0
        for head, fallbacks in sweep_groups:
            left = budget_left()
            if left is not None and left <= 0:
                break
            try:
                rows, _search_param = collect_term(service_key, head, fallbacks, args.page_size, left)
                consecutive = 0
                succeeded.add(head)
            except RuntimeError as exc:
                stats["term_failures"] += 1
                consecutive += 1
                print(f"검색어 수집 실패({head}): {exc}")
                if consecutive >= TERM_FAILURE_LIMIT:
                    print("연속 실패로 수집을 중단합니다. 지금까지 저장한 품목은 유지됩니다.")
                    break
                continue
            for row in rows:
                left = budget_left()
                if left is not None and left <= 0:
                    break
                process_row(row)
        return succeeded

    sync = load_sync()
    has_items = ITEMS_DIR.is_dir() and any(ITEMS_DIR.glob("*.json"))
    end_date = today_kst()

    if args.full or sync is None or not has_items:
        # 최초 구축·재구축: 전체 검색어 수집. 증분 인자 없는 호출은 시간당
        # 100회 제한 대상이라 평시에는 아래 변경분 경로를 쓴다.
        succeeded = sweep(groups)
        if stats["fetched"] == 0 and stats["term_failures"]:
            _print_summary(stats)
            return 1  # 아무것도 수집하지 못한 채 실패만 났다면 크게 실패한다
        save_sync(end_date, succeeded)
    else:
        seen_heads = set(sync.get("seen_heads") or [])
        # 1) 새 고시로 들어온 검색어만 검색한다 (건수가 적어 제한과 무관)
        succeeded = sweep([(head, fb) for head, fb in groups if head not in seen_heads])
        # 2) 변경일자 구간 질의로 기존 품목 갱신과 유관 신규 허가를 받는다
        try:
            changed = collect_changed(
                service_key, str(sync["last_change_date"]), end_date, args.page_size,
            )
        except RuntimeError as exc:
            print(f"변경분 조회 실패: {exc}")
            _print_summary(stats)
            return 1
        known = frozenset(path.stem for path in ITEMS_DIR.glob("*.json"))
        ingredients = stored_ingredients(ITEMS_DIR)
        for row in changed:
            seq = str(row.get("ITEM_SEQ") or "").strip()
            if seq in known or (base_ingredient_set(row) & ingredients):
                left = budget_left()
                if left is not None and left <= 0:
                    break
                process_row(row)
        save_sync(end_date, seen_heads | succeeded)

    _print_summary(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
