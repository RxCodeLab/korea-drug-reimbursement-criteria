"""공공데이터포털 의약품 제품 허가정보 수집기.

전량 열거(조건 없는 질의를 pageNo로 넘기며 numOfRows=500씩 받는다) 뒤
`mfds_match`로 고시 검색어와 로컬 매칭한 품목만 data/mfds/items/<ITEM_SEQ>.json에
개정 이력을 병합한다(--full, 최초 구축·주간 재구축). 평일 실행은 검색어 API 대신
변경일자 구간 질의(collect_changed)로 받은 변경분을 같은 로컬 매칭으로 걸러 갱신한다.
DATA_GO_KEY가 없으면 한 줄 안내 후 건너뛴다.
"""

import argparse
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock

from common import (
    DATA, DATE_YYYYMMDD, atomic_json, http_get, parse_changes_since, redact_text, today_kst,
)
from mfds_match import build_index, match_terms

API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06"
DETAIL_URL = "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail"
HISTORY_URL = "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemChangeHistList"
NORMALIZED_DIR = DATA / "normalized"
ITEMS_DIR = DATA / "mfds" / "items"
SCHEMA_VERSION = 1
DEFAULT_PAGE_SIZE = 100
DEFAULT_INCREMENTAL_WORKERS = 8
# 요청 간 대기 없음. 차단이 잦아지면 이 값을 올려 완화한다(예: 0.3).
REQUEST_SLEEP = 0.0
RESULT_OK = "00"
HISTORY_FAILURE_LIMIT = 5
# 변경분 처리 뒤 이력이 없는 기존 품목을 실행당 이만큼 더 백필한다. 실패한 품목이 변경분에 다시
# 나타나지 않으면 영원히 방치되던 문제를 막는다.
DEFAULT_HISTORY_BACKFILL_LIMIT = 200
# 공공데이터포털 numOfRows 상한(1000 이상은 코드=11 오류). 전량 열거는 이 값으로 42,985건을 86회 호출로 받는다(실측).
UNIVERSE_PAGE_SIZE = 500
TERM_FAILURE_DISPLAY_LIMIT = 20

CLASS_HEADER = re.compile(r"^\[[^\]]*\]\s*")
PUMMYEONG = re.compile(r"\(\s*품명\s*[:∶]?\s*([^)]*)\)")
ATTACHMENT_NAME = re.compile(r"\.(hwpx?|pdf|docx?|xlsx?|zip|txt)$", re.IGNORECASE)
WHITESPACE = re.compile(r"\s+")
LATIN = re.compile(r"[A-Za-z0-9]")
FORM_SUFFIX = re.compile(
    r"\s+(?:경구제|주사제|외용제|흡입제|점안제|비강분무제|좌제|연고제|액제|패취제|서방형제제)$"
)
HISTORY_TABLE_ID = "hist_list"
HISTORY_ONCLICK = re.compile(r"detailHist\(\s*'([^']+)'\s*,\s*'(\d{4}-\d{2}-\d{2})'")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _TextCollector(HTMLParser):
    """HTML 조각에서 텍스트 노드만 모은다. `<[^>]+>` 정규식과 달리 `CrCl < 30 또는 > 60` 같은 부등식을 지우지 않는다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_text(fragment: str) -> str:
    collector = _TextCollector()
    collector.feed(fragment)
    collector.close()
    return " ".join(collector.parts)


def normalize_ee(raw: object) -> str:
    text = html.unescape(str(raw or ""))
    stripped = text.lstrip()
    if stripped.startswith("<?xml") or stripped.startswith("<DOC"):
        try:
            # XML 파싱이 되면 itertext가 이미 순수 텍스트다. 여기에 태그 제거를 다시 걸면 본문의 부등식이 사라진다.
            text = " ".join(ET.fromstring(text).itertext())
            return WHITESPACE.sub(" ", text).strip()
        except ET.ParseError:
            pass
    return WHITESPACE.sub(" ", _html_text(text)).strip()


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def revision_key(text: str) -> str:
    """개정 동일성 키. API 본문과 변경이력 본문은 띄어쓰기만 다르게 오는 경우가 있어(저장 품목의 1%)
    해시만 비교하면 같은 효능·효과가 개정 두 건으로 보인다. 저장 해시는 그대로 두고 비교에만 쓴다."""
    return WHITESPACE.sub("", text)


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
    라틴 문자가 없는 앞머리)는 버린다. 품명 괄호 안 예시는 쉼표/등 기준으로 쪼개
    item_name fallback 검색어로 쓴다. 대소문자를 무시하고 중복을 제거한다.
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


class _HistoryTableParser(HTMLParser):
    """의약품안전나라 변경이력 페이지의 `<table id="hist_list">` 안 개정 링크를 읽는다.

    표 자체가 없으면 차단·점검 페이지거나 마크업이 바뀜 것이므로 호출자가 실패로 처리한다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_found = False
        self._depth = 0
        self.entries: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            if attributes.get("id") == HISTORY_TABLE_ID:
                self.table_found = True
                self._depth = 1
            elif self._depth:
                self._depth += 1
            return
        if tag == "a" and self._depth:
            document = attributes.get("data-docdata")
            onclick = HISTORY_ONCLICK.search(attributes.get("onclick") or "")
            if document is not None and onclick:
                self.entries.append((document, onclick.group(1), onclick.group(2)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._depth:
            self._depth -= 1


def parse_history(raw: bytes, item_seq: str, observed_at: str) -> list[dict]:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"MFDS 허가이력 응답 파싱 실패: {exc}") from exc
    parser = _HistoryTableParser()
    parser.feed(source)
    parser.close()
    if not parser.table_found:
        raise RuntimeError(f"MFDS 허가이력 페이지를 인식하지 못했습니다(변경이력 표 없음): itemSeq={item_seq}")
    revisions: list[dict] = []
    seen: set[str] = set()
    for document, official_id, official_date in parser.entries:
        text = normalize_ee(document)
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
        if total and len(collected) >= total:
            return collected
        if not rows or len(rows) < page_size:
            if total:
                # 전체 건수보다 적게 받았는데 페이지가 끝났다는 건 응답이 불완전하다는 뜻이다.
                # 조용히 성공으로 넘기면 누락된 채 동기화 날짜가 전진한다.
                raise RuntimeError(
                    f"MFDS API 응답이 불완전합니다: {len(collected)}건 수신, 전체 {total}건 ({query})"
                )
            return collected
        page_no += 1


def collect_changed(
    service_key: str, start_date: str, end_date: str, page_size: int,
) -> list[dict]:
    """변경일자 구간의 품목 변경분을 받아온다.

    평일 증분은 전량 열거 대신 이 변경분 질의를 쓴다. 실측으로 확인된 제약은 호출
    인자 유무가 아니라 호출 횟수·일일 트래픽 쿼터다 — 증분 인자 없는 반복 호출이
    시간당 100회로 막힌다는 기존 주석은 실측과 다르다(86회 연속 호출에 실패 0). 이 경로는
    날짜 질의로만 잡히는 6,205건(CHANGE_DATE 공란)을 영원히 놓치므로 주 1회 전량 열거(--full)가
    따로 필요하다.
    """
    return collect_query(
        service_key,
        {"start_change_date": start_date, "end_change_date": end_date},
        page_size, None,
    )


def collect_universe(service_key: str, page_size: int = UNIVERSE_PAGE_SIZE) -> list[dict]:
    """조건 없는 질의(`{}`)로 전 품목을 열거한다.

    첫 응답의 totalCount를 진실로 삼고, 수집한 고유 ITEM_SEQ 수가 그것과 다르면
    RuntimeError로 실패한다(조용한 누락 금지). numOfRows 상한은 500(실측: 1000 이상은
    코드=11 오류). 42,985건 기준 86회 호출로 끝난다.

    종료 조건은 "빈 페이지 또는 부족한 페이지" 하나만 믿지 않는다. 범위 밖 페이지에서
    API가 꽉 찬 페이지를 계속 주면(마지막 페이지 반복, 순환, 오류 페이로드를 200으로 반환)
    무한 루프가 될 수 있고, 그 경우 건수 불일치 검사는 루프 뒤에 있어 도달하지도 못한다.
    그래서 두 가지 강제 종료 장치를 둔다:
    1) 페이지 상한 — totalCount/page_size로 기대 페이지 수를 구하고 여유를 더한다. 여유의
       최소값 2는 totalCount가 page_size의 정확한 배수일 때 정상 경로에서도 빈 페이지를
       확인하려 한 번 더 호출하는 것을 흡수하기 위함이고, 나머지(기대 페이지의 10%)는
       ~8분짜리 수집 도중 신규 품목이 등록돼 totalCount가 소폭 늘어나는 경우의 페이지
       증가분을 흡수하기 위함이다.
    2) 진전 없음 감지 — 한 페이지를 다 처리했는데 고유 ITEM_SEQ 수가 하나도 안 늘면
       같은 페이지 반복이나 순환으로 보고 즉시 실패한다.
    """
    collected: list[dict] = []
    seen: set[str] = set()
    page_no = 1
    total = None
    max_pages = None
    while True:
        body = fetch_page(service_key, {}, page_no, page_size)
        time.sleep(REQUEST_SLEEP)
        rows = body_items(body)
        if total is None:
            try:
                total = int(body.get("totalCount") or 0)
            except (TypeError, ValueError):
                total = 0
            expected_pages = max(1, -(-total // page_size))
            max_pages = expected_pages + max(2, expected_pages // 10)
        before = len(seen)
        for row in rows:
            seq = str(row.get("ITEM_SEQ") or "").strip()
            if seq and seq not in seen:
                seen.add(seq)
                collected.append(row)
        if not rows or len(rows) < page_size:
            break
        if len(seen) == before:
            raise RuntimeError(
                f"MFDS 전량 열거 진전 없음: pageNo={page_no}에서 새 ITEM_SEQ 0건 "
                f"(누적 {len(seen)}건, totalCount={total}건) — 같은 페이지 반복 또는 순환 의심"
            )
        page_no += 1
        if page_no > max_pages:
            raise RuntimeError(
                f"MFDS 전량 열거 페이지 상한 초과: pageNo={page_no} > 상한 {max_pages} "
                f"(누적 {len(seen)}건, totalCount={total}건) — 범위 밖 페이지가 정상 응답을 반환하는 것으로 의심"
            )
    if len(seen) != total:
        raise RuntimeError(
            f"MFDS 전량 열거 건수 불일치: 고유 {len(seen)}건 수집, totalCount={total}건"
        )
    return collected


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


def merge_item(item: dict, items_dir: Path, observed_at: str) -> str:
    """항목 1건을 items_dir/<ITEM_SEQ>.json에 병합한다.

    최초 수집은 "new", 현행과 동일한 내용 재수집은 last_observed_at만 갱신해
    "unchanged", 내용이 바뀌면 "changed"를 반환한다. 개정은 해시 하나당 객체 하나다:
    이미 아는 해시가 다시 현행이 되면(A→B→A) 그 객체를 맨 앞으로 옮기고
    official_revision_date 같은 공식 메타데이터는 보존한다.
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
    ee_doc_id = str(item.get("EE_DOC_ID") or "").strip()
    revisions = [r for r in (existing or {}).get("revisions") or [] if isinstance(r, dict)]
    key = revision_key(ee_text)
    known = next(
        (r for r in revisions if r.get("content_sha256") == digest or revision_key(str(r.get("ee_text") or "")) == key),
        None,
    )
    if known is None:
        status = "changed" if revisions else "new"
        revisions.insert(0, {
            "revision_id": f"{seq}-{digest[:8]}",
            "content_sha256": digest,
            "ee_text": ee_text,
            "ee_doc_id": ee_doc_id,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
        })
    else:
        status = "unchanged" if revisions[0] is known else "changed"
        known["last_observed_at"] = observed_at
        if not known.get("ee_doc_id"):
            known["ee_doc_id"] = ee_doc_id
        if revisions[0] is not known:
            revisions.remove(known)
            revisions.insert(0, known)
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

    API가 현행으로 보고한 개정에는 official_revision_date가 없어 날짜 역순 정렬에서
    과거 개정에 밀리므로, 정렬 뒤에도 맨 앞에 오도록 고정한다. 수집 시각을
    history_fetched_at에 남겨 다음 실행이 재수집 대상을 판단하게 한다.
    """
    path = items_dir / f"{item_seq}.json"
    item = json.loads(path.read_text(encoding="utf-8"))
    revisions = item["revisions"]
    current_hash = revisions[0]["content_sha256"] if revisions else ""
    by_key = {revision_key(revision["ee_text"]): revision for revision in revisions}
    added = 0
    for official in history:
        existing = by_key.get(revision_key(official["ee_text"]))
        if existing is not None:
            existing["ee_doc_id"] = official["ee_doc_id"]
            existing["official_revision_date"] = official["official_revision_date"]
            continue
        revisions.append(official)
        by_key[revision_key(official["ee_text"])] = official
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
BACKFILL_PATH = DATA / "mfds" / "backfill.json"


def load_sync() -> dict | None:
    """마지막 변경분 동기화 상태를 읽는다. 없거나 손상되었으면 None.

    구버전 파일의 `seen_heads` 필드는 조용히 무시한다(검색어 sweep이 사라지면서 더 이상 쓰이지 않는다).
    """
    try:
        sync = json.loads(SYNC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(sync, dict) or not str(sync.get("last_change_date") or "").strip():
        return None
    return sync


def save_sync(last_change_date: str, last_full_run: str | None) -> None:
    """동기화 상태를 쓴다. `last_full_run`은 마지막 전량 열거(--full) 시각(UTC ISO)이다 —
    평일 증분만으로는 CHANGE_DATE가 빈 6,205건을 영원히 놓치므로, 이 값으로 전량 이후 경과시간을
    판단한다.
    """
    atomic_json(SYNC_PATH, {
        "last_change_date": last_change_date,
        "last_full_run": last_full_run,
    })


def load_backfill(start_date: str) -> dict | None:
    """같은 시작일로 중단된 백필이 있으면 다음 미완료 구간을 돌려준다."""
    try:
        state = json.loads(BACKFILL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(state, dict)
        and state.get("start_date") == start_date
        and DATE_YYYYMMDD.fullmatch(str(state.get("next_date") or ""))
        and DATE_YYYYMMDD.fullmatch(str(state.get("end_date") or ""))
    ):
        return state
    return None


def save_backfill(start_date: str, next_date: str, end_date: str) -> None:
    atomic_json(BACKFILL_PATH, {
        "start_date": start_date,
        "next_date": next_date,
        "end_date": end_date,
    })


def month_window(start_date: str, end_date: str) -> tuple[str, str]:
    """start_date가 속한 달의 말일까지, 단 전체 종료일을 넘지 않는 구간."""
    start = datetime.strptime(start_date, "%Y%m%d").date()
    first_next_month = (
        start.replace(year=start.year + 1, month=1, day=1)
        if start.month == 12 else start.replace(month=start.month + 1, day=1)
    )
    window_end = min(first_next_month - timedelta(days=1),
                     datetime.strptime(end_date, "%Y%m%d").date())
    return start.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")


def next_date(value: str) -> str:
    return (datetime.strptime(value, "%Y%m%d").date() + timedelta(days=1)).strftime("%Y%m%d")


def match_relevant_seqs(rows: list[dict], groups: list[tuple[str, list[str]]]) -> set[str]:
    """행 목록에 mfds_match 규칙을 적용해 고시 검색어에 걸리는 ITEM_SEQ만 골라낸다.

    변경분 행만으로 새 색인을 만들어 매번 다시 매칭한다(전량 우주 색인과 달리 건수가
    적어 스캐니용 색인 재사용이 더 비심).
    """
    return match_terms(build_index(rows), groups).matched


def _print_summary(stats: dict, failed_terms: list[str] | None = None) -> None:
    print(
        f"[MFDS] 수집 항목={stats['fetched']}건, 신규 개정={stats['new']}건, "
        f"과거 허가이력={stats['history']}건, 변동 없음={stats['unchanged']}건, "
        f"이력 미수집={stats['history_skipped']}건, 매칭 실패={stats['match_failures']}건"
    )
    if failed_terms:
        shown = failed_terms[:TERM_FAILURE_DISPLAY_LIMIT]
        print(
            f"[MFDS] 매칭 실패 검색어 {len(failed_terms)}건(상위 {len(shown)}건): {shown}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="공공데이터포털 의약품 제품 허가정보 수집")
    parser.add_argument("--max-terms", type=int, default=None, help="검색어 상한(기본: 전체)")
    parser.add_argument("--max-items", type=int, default=None, help="저장 항목 상한(기본: 제한 없음)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="페이지당 건수")
    parser.add_argument("--incremental-workers", type=int, default=DEFAULT_INCREMENTAL_WORKERS,
                        help="변경분 품목 병렬 처리 수(기본: 8)")
    parser.add_argument("--changes-since",
                        help="지정일(YYYYMMDD)부터 변경분을 다시 조회해 과거 누락을 복구")
    parser.add_argument("--skip-history", action="store_true", help="의약품안전나라 효능·효과 변경이력 수집 생략")
    parser.add_argument("--history-backfill-limit", type=int, default=DEFAULT_HISTORY_BACKFILL_LIMIT,
                        help="변경분 처리 뒤 이력이 없는 기존 품목을 이만큼 추가 백필(기본: 200)")
    parser.add_argument("--full", action="store_true",
                        help="변경분 대신 전체 검색어로 수집(최초 구축·재구축용)")
    args = parser.parse_args(argv)
    for name in ("max-terms", "max-items", "page-size", "incremental-workers", "history-backfill-limit"):
        value = getattr(args, name.replace("-", "_"))
        if value is not None and value < 1:
            parser.error(f"--{name}은(는) 1 이상이어야 합니다")
    if args.changes_since:
        try:
            parse_changes_since(args.changes_since)
        except ValueError as exc:
            parser.error(str(exc))
    if args.full and args.changes_since:
        parser.error("--full과 --changes-since는 함께 사용할 수 없습니다")
    if args.max_items and args.changes_since:
        parser.error("--max-items와 --changes-since는 함께 사용할 수 없습니다")

    service_key = os.environ.get("DATA_GO_KEY")
    if not service_key:
        print("DATA_GO_KEY 환경 변수가 설정되지 않아 의약품 허가정보 수집을 건너뜁니다.")
        return 0
    service_key = urllib.parse.unquote(service_key)

    groups = term_groups_from_titles(load_titles(NORMALIZED_DIR))
    if args.max_terms is not None:
        groups = groups[: args.max_terms]

    stats = {"fetched": 0, "new": 0, "history": 0, "unchanged": 0,
             "history_skipped": 0, "match_failures": 0}
    history_state = {"enabled": not args.skip_history, "failures": 0}
    seen: set[str] = set()
    state_lock = Lock()

    def budget_left() -> int | None:
        return None if args.max_items is None else args.max_items - stats["fetched"]

    def budget_exhausted() -> bool:
        left = budget_left()
        return left is not None and left <= 0

    def collect_history(seq: str, observed_at: str) -> None:
        """허가이력 1건을 받아 병합한다.

        실패는 품목 단위로 넘긴다. history_fetched_at 마커가 남지 않으므로 해당 품목은
        다음 실행의 백필 대상이 되고, 연속 실패가 이어지면 이번 실행의 이력 수집만
        중단해 품목 수집을 지킨다.
        """
        try:
            history_count = merge_history(seq, fetch_history(seq, observed_at), ITEMS_DIR, observed_at)
            with state_lock:
                stats["history"] += history_count
                history_state["failures"] = 0
        except RuntimeError as exc:
            with state_lock:
                history_state["failures"] += 1
                stats["history_skipped"] += 1
                stop_history = history_state["failures"] >= HISTORY_FAILURE_LIMIT
                if stop_history:
                    history_state["enabled"] = False
            print(f"허가이력 수집 실패({seq}): {exc}")
            if stop_history:
                print("연속 실패로 이번 실행의 허가이력 수집을 중단합니다. "
                      "미수집 품목은 다음 실행에서 백필합니다.")

    def process_row(row: dict) -> None:
        seq = str(row.get("ITEM_SEQ") or "").strip()
        with state_lock:
            if not seq or seq in seen:
                return
            seen.add(seq)
        observed_at = now_utc()
        result = merge_item(row, ITEMS_DIR, observed_at)
        with state_lock:
            stats["fetched"] += 1
            stats["unchanged" if result == "unchanged" else "new"] += 1
            history_enabled = history_state["enabled"]
        if history_enabled and (result != "unchanged" or history_pending(seq, ITEMS_DIR)):
            collect_history(seq, observed_at)

    def backfill_pending_history(limit: int) -> None:
        """변경분에 다시 나타나지 않는 이력 미수집 품목을 실행당 limit건까지 따로 백필한다."""
        for path in sorted(ITEMS_DIR.glob("*.json")):
            if limit <= 0 or not history_state["enabled"]:
                return
            seq = path.stem
            if seq in seen or not history_pending(seq, ITEMS_DIR):
                continue
            limit -= 1
            with state_lock:
                seen.add(seq)
            collect_history(seq, now_utc())

    sync = load_sync()
    has_items = ITEMS_DIR.is_dir() and any(ITEMS_DIR.glob("*.json"))
    end_date = today_kst()
    previous_full_run = str((sync or {}).get("last_full_run") or "") or None

    def finish(save_state, failed_terms=None):
        if budget_exhausted():
            print("--max-items 상한에 도달해 동기화 상태를 갱신하지 않습니다. 다음 실행이 같은 범위를 다시 처리합니다.")
        else:
            save_state()
        if history_state["enabled"] and args.history_backfill_limit:
            backfill_pending_history(args.history_backfill_limit)
        _print_summary(stats, failed_terms)
        return 0

    if args.full or (not args.changes_since and (sync is None or not has_items)):
        try:
            universe = collect_universe(service_key)
        except RuntimeError as exc:
            print(f"전량 열거 실패: {exc}")
            return 1
        match_result = match_terms(build_index(universe), groups)
        stats["match_failures"] = len(match_result.failed_terms)
        by_seq = {str(row.get("ITEM_SEQ") or ""): row for row in universe}
        for seq in sorted(match_result.matched):
            if budget_exhausted():
                break
            row = by_seq.get(seq)
            if row is not None:
                process_row(row)
        return finish(lambda: save_sync(end_date, now_utc()), match_result.failed_terms)

    def process_changed_range(start_date: str, range_end: str) -> bool:
        try:
            changed = collect_changed(
                service_key, start_date, range_end, args.page_size,
            )
        except RuntimeError as exc:
            print(f"변경분 조회 실패({start_date}~{range_end}): {exc}")
            return False
        known = frozenset(path.stem for path in ITEMS_DIR.glob("*.json"))
        matched = match_relevant_seqs(changed, groups)
        eligible = []
        for row in changed:
            seq = str(row.get("ITEM_SEQ") or "").strip()
            if seq and (seq in known or seq in matched):
                eligible.append(row)
                left = budget_left()
                if left is not None and len(eligible) >= left:
                    break
        with ThreadPoolExecutor(max_workers=args.incremental_workers) as executor:
            list(executor.map(process_row, eligible))
        return True

    if args.changes_since:
        checkpoint = load_backfill(args.changes_since)
        backfill_end = str(checkpoint["end_date"]) if checkpoint else end_date
        cursor = str(checkpoint["next_date"]) if checkpoint else args.changes_since
        if not checkpoint:
            save_backfill(args.changes_since, cursor, backfill_end)
        while cursor <= backfill_end:
            range_start, range_end = month_window(cursor, backfill_end)
            if not process_changed_range(range_start, range_end):
                _print_summary(stats)
                return 1
            cursor = next_date(range_end)
            save_backfill(args.changes_since, cursor, backfill_end)
        BACKFILL_PATH.unlink(missing_ok=True)
        previous_sync_date = str((sync or {}).get("last_change_date") or "")
        return finish(lambda: save_sync(max(previous_sync_date, backfill_end), previous_full_run))

    if not process_changed_range(str((sync or {})["last_change_date"]), end_date):
        _print_summary(stats)
        return 1
    return finish(lambda: save_sync(end_date, previous_full_run))


if __name__ == "__main__":
    raise SystemExit(main())
