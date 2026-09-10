from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
from urllib.parse import quote

from html import escape as html_escape

from common import BASE, DATA
from search import ACTION_LABELS, ROLE_ORDER

ASSETS = BASE / "assets"
NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
COLLECTION_STATUS_PATH = DATA / "collection.json"
PUBLIC = BASE / "public"
SITE_URL = "https://rxcodelab.github.io/korea-drug-reimbursement-criteria/"
REPO_URL = "https://github.com/RxCodeLab/korea-drug-reimbursement-criteria"
FOOTER_LABEL_PLACEHOLDER = "__FOOTER_LABEL__"
MAX_BODY_CHARS = 50_000
# 허가 색인 행의 열 순서. 브라우저는 이 순서로 객체를 복원한다. 수만 행(전량 수집 기준 약 4만건)이라 키 이름을 행마다 싣지 않는다.
MFDS_INDEX_FIELDS = (
    "item_seq", "item_name", "entp_name", "main_item_ingr", "main_item_ingr_eng",
    "permit_date", "revision_count", "content_key",
)
MFDS_CONTENT_KEY_CHARS = 12
# 값이 행 수보다 훨씬 적게 반복되는 열은 사전으로 빼고 행에는 정수 인덱스만 싣는다
# (실측: 43,017행에 제조사 595종·성분 7,641종·영문성분 5,650종). 전송량 gzip 1.96→1.30MB,
# 브라우저 로드 시간 32% 감소. 브라우저는 dicts로 원래 값을 되돌린다.
MFDS_INDEX_DICT_FIELDS = ("entp_name", "main_item_ingr", "main_item_ingr_eng", "permit_date", "content_key")
# ee_text에 남은 진짜 HTML 태그와 미해제 엔티티. normalize_ee가 v3에서 둘 다 정리하므로
# 재수집된 레코드에는 남지 않지만, 아직 재수집 안 된 예전 레코드에는 남아 있을 수 있다.
# 태그·엔티티를 구분할 필요가 없어 한 패턴으로 합쳐 레코드당 본문을 한 번만 훑는다.
# (실측 2026-09-09: 저장된 43,017건 모두 깨끗해 현재는 아무것도 걸러내지 않는다.)
EE_MARKUP_LEFTOVER = re.compile(r"<[a-zA-Z/][^>]*>|&[a-zA-Z#][a-zA-Z0-9]*;")


def load_normalized() -> list[dict]:
    """정규화 문서를 한 번만 읽는다. 색인·목록·상세 페이지·최근 갱신 문구가 모두 이 목록을 쓴다."""
    if not NORMALIZED.is_dir():
        return []
    documents = []
    for path in sorted(NORMALIZED.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("complete") is not True:
            raise RuntimeError(f"내용이 온전하지 않은 정규화 문서입니다: {path}")
        documents.append(document)
    return documents


def build_index(documents: list[dict]) -> list[dict]:
    records: list[dict] = []
    for document in documents:
        version = document["version"]
        attachments = {a["ordinal"]: a for a in document["attachments"]}
        for entry in document["entries"]:
            source = attachments[entry["attachment_ordinal"]]
            body = entry["body"]
            records.append({
                "key": entry["block_identity"],
                "title": entry["title"],
                "body": body[:MAX_BODY_CHARS],
                "truncated": len(body) > MAX_BODY_CHARS,
                "action": entry["action"],
                "class_no": entry["class_no"],
                "class_header": entry["class_header"],
                "effective_date": version["시행일자"],
                "notice_number": version["발령번호"],
                "sequence": version["행정규칙일련번호"],
                "source_name": source["original_name"],
                "source_sha256": source["sha256"],
                "role": "" if entry["class_no"] else source["role"],
                "ordinal": source["ordinal"],
            })
    return sorted(records, key=lambda r: (r["key"], r["effective_date"], r["sequence"], r["source_sha256"]))


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def has_clean_current_text(record: dict) -> bool:
    """현행(revisions[0]) 효능·효과 본문이 게시 가능한 상태인지 판별한다.

    수집 범위가 고시 매칭 품목에서 식약처 허가 전량으로 바뀌면서(사용자 결정: 급여
    여부와 무관하게 모두 게시), 수집된 모든 품목을 게시한다. 유일한 예외는 normalize_ee
    구버전(v3 이전)이 만든 텍스트가 아직 재수집되지 않은 채 남아 그 문자열 자체가
    깨져 있는 경우다(잔존 판별은 EE_MARKUP_LEFTOVER 참고). 재수집되면 텍스트가 갱신되어
    자동으로 게시 대상에 들어온다 — 저장 파일 자체는 지우지 않아 이 함수가 다음 빌드에서
    자동으로 다시 포함한다.
    """
    revisions = record.get("revisions") or []
    if not revisions:
        return False
    return not EE_MARKUP_LEFTOVER.search(str(revisions[0].get("ee_text") or ""))


def encode_mfds_index(rows: list[list[object]]) -> dict:
    """행 목록을 사전 + 정수 인덱스 형태로 압축한다(MFDS_INDEX_DICT_FIELDS 참고)."""
    positions = [(field, MFDS_INDEX_FIELDS.index(field)) for field in MFDS_INDEX_DICT_FIELDS]
    tables: dict[str, list[object]] = {field: [] for field in MFDS_INDEX_DICT_FIELDS}
    lookups: dict[str, dict[object, int]] = {field: {} for field in MFDS_INDEX_DICT_FIELDS}
    encoded = []
    for row in rows:
        packed = list(row)
        for field, position in positions:
            value = row[position]
            lookup = lookups[field]
            if value not in lookup:
                lookup[value] = len(tables[field])
                tables[field].append(value)
            packed[position] = lookup[value]
        encoded.append(packed)
    return {"fields": list(MFDS_INDEX_FIELDS), "dicts": tables, "rows": encoded}


def build_mfds_public() -> list[list[object]]:
    """허가 품목 색인(열 배열)과 품목별 상세 JSON을 쓴다.

    수집된(=EE_DOC_DATA가 있는) 모든 식약처 허가 품목을 게시한다 — 급여기준과
    연결되는지는 더 이상 게시 조건이 아니다(사용자 결정). 게시 대상 판별은
    has_clean_current_text 참고.
    """
    rows: list[list[object]] = []
    output = PUBLIC / "mfds"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if not MFDS_ITEMS.is_dir():
        (output / "search-index.json").write_text(_compact_json(encode_mfds_index(rows)), encoding="utf-8")
        return rows
    items_dir = output / "items"
    for path in sorted(MFDS_ITEMS.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("complete") is not True:
            raise RuntimeError(f"내용이 온전하지 않은 MFDS 항목입니다: {path}")
        if not has_clean_current_text(record):
            continue
        revisions = record.get("revisions") or []
        rows.append([
            record["item_seq"],
            record["item_name"],
            record["entp_name"],
            record["main_item_ingr"],
            record.get("main_item_ingr_eng", ""),
            record["permit_date"],
            len(revisions),
            revisions[0]["content_sha256"][:MFDS_CONTENT_KEY_CHARS] if revisions else "",
        ])
        target = items_dir / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_compact_json(record), encoding="utf-8")
    rows.sort(key=lambda r: (r[1], r[0]))
    (output / "search-index.json").write_text(_compact_json(encode_mfds_index(rows)), encoding="utf-8")
    return rows


HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>약제 급여기준 변경 이력 검색</title>
<meta name="description" content="약제명과 성분명으로 보건복지부 약제 급여기준의 신설·변경·삭제 이력, 시행일과 고시번호를 검색합니다.">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:title" content="약제 급여기준 변경 이력 검색">
<meta property="og:description" content="약제명과 성분명으로 시행일별 급여기준 변경 내용을 검색합니다.">
<meta property="og:url" content="https://rxcodelab.github.io/korea-drug-reimbursement-criteria/">
<link rel="canonical" href="https://rxcodelab.github.io/korea-drug-reimbursement-criteria/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebSite","name":"약제 급여기준 변경 이력 검색","url":"https://rxcodelab.github.io/korea-drug-reimbursement-criteria/","description":"약제명과 성분명으로 보건복지부 약제 급여기준의 신설·변경·삭제 이력을 검색합니다.","inLanguage":"ko","potentialAction":{"@type":"SearchAction","target":{"@type":"EntryPoint","urlTemplate":"https://rxcodelab.github.io/korea-drug-reimbursement-criteria/?q={search_term_string}"},"query-input":"required name=search_term_string"}}</script>
<style>
body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1d2433}input{width:100%;box-sizing:border-box;padding:.8rem;font-size:1rem;border:1px solid #8993a4;border-radius:6px}.hint,.meta{color:#5b6575}.group{margin:1.5rem 0;border-top:2px solid #28364d}.group h2{font-size:1.15rem}details{border:1px solid #ccd2dc;border-radius:6px;margin:.5rem 0;padding:.5rem .8rem}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:.8rem}.badge{color:#a33;margin-left:.5rem}.revision{font-weight:700;margin:.6rem 0 .2rem}.empty{padding:2rem 0;color:#5b6575}.more{display:block;width:100%;margin:.8rem 0;padding:.7rem;font:inherit;color:#1d2433;background:#eef1f6;border:1px solid #ccd2dc;border-radius:6px;cursor:pointer}.more:hover{background:#e2e7ef}.related{margin-top:2rem;border-color:#8993a4}.related>.group{margin-left:.5rem}.catalog{margin-top:2rem;color:#5b6575;font-size:.9rem}.catalog ul{columns:2;margin:.5rem 0;padding-left:1.2rem}footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid #ccd2dc;color:#5b6575;font-size:.9rem}
</style></head><body>
<h1>약제 급여기준 변경 이력 검색</h1><p class="hint">한글 또는 영문 검색어를 공백으로 나누어 입력하면, 하나라도 포함된 항목을 표시합니다.</p>
<input id="q" type="search" autocomplete="off" placeholder="예: dapagliflozin 다파글리플로진" autofocus><p id="status" class="meta"></p><main id="results"></main>
__DRUG_CATALOG__
<footer>__FOOTER_LABEL__<a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">데이터 수집·검증 과정 보기</a></footer>
<script>
__SEARCH_JS__
</script></body></html>'''


def latest_notice_label(documents: list[dict]) -> str:
    """수록된 가장 최근 발령 고시로 '최근 갱신' 문구를 만든다. 자료가 없으면 빈 문자열."""
    latest = ("", "")
    for document in documents:
        version = document.get("version") or {}
        key = (str(version.get("발령일자") or ""), str(version.get("발령번호") or ""))
        if key[0] > latest[0]:
            latest = key
    if not latest[0]:
        return ""
    date = f"{latest[0][:4]}-{latest[0][4:6]}-{latest[0][6:8]}"
    return f"최근 갱신: {date} · "


def last_success_label() -> str:
    """data/collection.json에 워크플로가 기록한 마지막 수집 성공 날짜. 실패·시도 정보는 절대 담지 않는다.

    파일이 없으면(최초 실행, 기존 저장소) 빈 문자열 — 이 부분을 생략하고 조용히 넘어간다.
    """
    if not COLLECTION_STATUS_PATH.is_file():
        return ""
    try:
        status = json.loads(COLLECTION_STATUS_PATH.read_text(encoding="utf-8"))
        raw_date = str(status["last_success_date"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise RuntimeError(f"{COLLECTION_STATUS_PATH} 형식이 잘못되었습니다") from None
    if not raw_date:
        return ""
    date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return f"마지막 수집 성공: {date} · "


SLUG_STRIP = re.compile("[^0-9a-z가-힣]+")


def slugify(title: str) -> str:
    """제목을 URL 슬러그로 만든다. 예) Dapagliflozin 경구제 → dapagliflozin-경구제"""
    slug = SLUG_STRIP.sub("-", title.casefold()).strip("-")
    return slug[:80].rstrip("-") or "criteria"


def identity_slug(block_identity: str) -> str:
    """항목 식별자(block_identity)로 만든 슬러그. 최신 품명이 바뀌어도 URL이 유지된다.

    NFKC로 Ⅷ→VIII 같은 호환 문자를 풀어 혐액응고인자 Ⅷ/Ⅸ처럼 로마숫자만 다른 식별자가 같은 슬러그로 무너지지 않게 한다.
    """
    return slugify(unicodedata.normalize("NFKC", block_identity).replace("[", "").replace("]", "-"))


def page_slugs(identities: list[str]) -> dict[str, str]:
    """식별자별 페이지 슬러그. 슬러그가 겹치는 식별자는 양쪽 모두 식별자 해시를 붙여, 정렬 순서와 무관하게 URL이 고정된다."""
    base = {identity: identity_slug(identity) for identity in identities}
    counts: dict[str, int] = {}
    for slug in base.values():
        counts[slug] = counts.get(slug, 0) + 1
    return {
        identity: slug if counts[slug] == 1 else f"{slug}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:6]}"
        for identity, slug in base.items()
    }


def criteria_groups(documents: list[dict]) -> list[list[dict]]:
    """항목(block_identity)별 급여기준 개정 전문을 최신 순으로 묶는다."""
    groups: dict[str, list[dict]] = {}
    for document in documents:
        version = document["version"]
        for entry in document["entries"]:
            if not entry["class_no"]:
                continue
            groups.setdefault(entry["block_identity"], []).append({
                "identity": entry["block_identity"],
                "title": entry["title"],
                "class_header": entry["class_header"],
                "action": entry["action"],
                "body": entry["body"],
                "effective_date": version["시행일자"],
                "notice_number": version["발령번호"],
                "sequence": version["행정규칙일련번호"],
            })
    ordered = [
        sorted(items, key=lambda r: (r["effective_date"], r["sequence"]), reverse=True)
        for items in groups.values()
    ]
    ordered.sort(key=lambda items: items[0]["title"].casefold())
    return ordered


def criteria_page(newest: dict, items: list[dict], url_path: str) -> str:
    """급여기준 한 항목의 정적 상세 페이지."""
    title = newest["title"]
    description = " ".join(items[0]["body"].split())[:150]
    canonical = SITE_URL + url_path
    parts = [
        '<!doctype html>',
        '<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html_escape(title)} 급여기준 변경 이력</title>",
        f'<meta name="description" content="{html_escape(description)}">',
        '<meta name="robots" content="index,follow">',
        '<meta property="og:type" content="article">',
        f'<meta property="og:title" content="{html_escape(title)} 급여기준 변경 이력">',
        f'<meta property="og:url" content="{html_escape(canonical)}">',
        f'<link rel="canonical" href="{html_escape(canonical)}">',
        '<style>body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;'
        'margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1d2433}'
        '.meta{color:#5b6575}article{border:1px solid #ccd2dc;border-radius:6px;'
        'margin:1rem 0;padding:.8rem}h2{font-size:1.05rem;margin:.2rem 0}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:.8rem}'
        'footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid #ccd2dc;'
        'color:#5b6575;font-size:.9rem}</style></head><body>',
        f"<h1>{html_escape(title)}</h1>",
    ]
    if newest["class_header"]:
        parts.append(f'<p class="meta">{html_escape(newest["class_header"])}</p>')
    parts.append(f'<p><a href="../?q={quote(title.split()[0])}">← 검색으로 돌아가기</a></p>')
    for record in items:
        date = record["effective_date"]
        notice_url = ("https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq="
                      + quote(str(record["sequence"])))
        parts.append(
            "<article>"
            f"<h2>{date[:4]}-{date[4:6]}-{date[6:8]} 시행 · "
            f"고시 제{html_escape(record['notice_number'])}호 · {html_escape(record['action'])}</h2>"
            f'<p class="meta"><a href="{notice_url}" target="_blank" rel="noopener">'
            "국가법령정보센터 원문</a></p>"
            f"<pre>{html_escape(record['body'])}</pre></article>"
        )
    parts.append("<footer>" + FOOTER_LABEL_PLACEHOLDER + '<a href="'
                 + REPO_URL + '">데이터 수집·검증 과정 보기</a></footer></body></html>')
    return chr(10).join(parts)


def build_criteria_pages(documents: list[dict], footer_label: str) -> list[tuple[str, str]]:
    """항목별 정적 페이지를 쓰고 (제목, 상대 URL) 목록을 돌려준다."""
    output = PUBLIC / "criteria"
    if output.exists():
        shutil.rmtree(output)
    entries: list[tuple[str, str]] = []
    groups = criteria_groups(documents)
    slugs = page_slugs([items[0]["identity"] for items in groups])
    for items in groups:
        newest = items[0]
        slug = slugs[newest["identity"]]
        url_path = f"criteria/{quote(slug)}.html"
        page = criteria_page(newest, items, url_path).replace(FOOTER_LABEL_PLACEHOLDER, footer_label)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{slug}.html").write_text(page + chr(10), encoding="utf-8")
        entries.append((newest["title"], url_path))
    return entries


def static_drug_list(catalog: list[tuple[str, str]]) -> str:
    """수록 약제 목록. 크롤러와 사용자가 여기서 항목 페이지로 들어간다."""
    if not catalog:
        return ""
    items = "".join(
        f'<li><a href="{path}">{html_escape(title)}</a></li>' for title, path in catalog
    )
    return (f'<details class="catalog"><summary>수록된 약제 급여기준 {len(catalog):,}건 목록</summary>'
            f"<ul>{items}</ul></details>")


def write_crawler_files(page_urls: list[str]) -> None:
    """sitemap.xml과 robots.txt를 생성한다.

    프로젝트 페이지여서 robots.txt가 루트에 놓이지 않아 크롤러에는 효력이 없지만,
    sitemap 위치를 문서화하는 용도로 함께 둔다. sitemap은 Search Console
    제출로 유효하다.
    """
    urls = "".join(f"<url><loc>{html_escape(url)}</loc></url>" for url in page_urls)
    sitemap_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        "",
    ]
    (PUBLIC / "sitemap.xml").write_text(chr(10).join(sitemap_lines), encoding="utf-8")
    robots_lines = ["User-agent: *", "Allow: /", "", f"Sitemap: {SITE_URL}sitemap.xml", ""]
    (PUBLIC / "robots.txt").write_text(chr(10).join(robots_lines), encoding="utf-8")


def search_script() -> str:
    """검색 스크립트를 인라인용 한 덩어리로 잇는다.

    core가 먼저 와야 한다 — ui가 첫 줄에서 SearchCore를 구조 분해한다. 파일을 따로 실어
    보내지 않고 인라인하는 이유는 요청을 늘리지 않기 위해서다(둘 합쳐 15KB 남짓).
    """
    return "\n".join((ASSETS / name).read_text(encoding="utf-8").strip()
                     for name in ("search-core.js", "search-ui.js"))


def render_index_page(catalog: list[tuple[str, str]], footer_label: str) -> str:
    role_ranks = {role: rank for rank, role in enumerate(ROLE_ORDER)}
    return (
        HTML.replace("__SEARCH_JS__", search_script())
        .replace("__ACTION_LABELS__", json.dumps(ACTION_LABELS, ensure_ascii=False, separators=(",", ":")))
        .replace("__ROLE_RANKS__", json.dumps(role_ranks, ensure_ascii=False, separators=(",", ":")))
        .replace(FOOTER_LABEL_PLACEHOLDER, footer_label)
        .replace("__DRUG_CATALOG__", static_drug_list(catalog))
    )


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    documents = load_normalized()
    index = build_index(documents)
    mfds_rows = build_mfds_public()
    (PUBLIC / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
    footer_label = latest_notice_label(documents) + last_success_label()
    catalog = build_criteria_pages(documents, footer_label)
    write_crawler_files([SITE_URL] + [SITE_URL + path for _, path in catalog])
    (PUBLIC / "index.html").write_text(render_index_page(catalog, footer_label) + "\n", encoding="utf-8")
    print(f"검색 항목 {len(index)}개, 식약처 허가 품목 {len(mfds_rows)}개(급여기준과 무관한 품목 포함), "
          f"기준 페이지 {len(catalog)}개를 {PUBLIC}에 생성했습니다.")


if __name__ == "__main__":
    main()
