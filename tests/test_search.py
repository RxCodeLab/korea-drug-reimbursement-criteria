import json

import pytest

import build_site
import search


@pytest.fixture(autouse=True)
def isolated_mfds_items(monkeypatch, tmp_path):
    """실데이터(data/mfds/items 1만4천 파일)를 읽지 않도록 격리한다."""
    monkeypatch.setattr(build_site, "MFDS_ITEMS", tmp_path / "no-mfds-items")


def normalized_document():
    return {
        "schema_version": 1,
        "complete": True,
        "version": {
            "시행일자": "20250101",
            "발령번호": "2024-1",
            "발령일자": "20241231",
            "행정규칙일련번호": "seq-1",
            "행정규칙명": "test",
        },
        "attachments": [
            {"ordinal": 1, "original_name": "별지.hwpx", "sha256": "a" * 64, "role": "annex"},
            {"ordinal": 2, "original_name": "고시개정문.hwp", "sha256": "b" * 64, "role": "notice"},
        ],
        "entries": [
            {
                "action": "변경",
                "class_no": "219",
                "class_header": "[219] 기타",
                "title": "Dapagliflozin 경구제",
                "body": "다파글리플로진 급여기준",
                "attachment_ordinal": 1,
                "attachment_sha256": "a" * 64,
                "block_identity": "[219]dapagliflozin경구제",
            },
            {
                "action": "notice",
                "class_no": "",
                "class_header": "",
                "title": "고시개정문.hwp",
                "body": "고시 개정 내용",
                "attachment_ordinal": 2,
                "attachment_sha256": "b" * 64,
                "block_identity": "__notice__" + "b" * 64,
            },
        ],
    }


def criterion_record(key, effective_date, sequence, *, title="기준 항목", class_header="", action="변경",
                     notice_number="2025-1"):
    return {
        "key": key, "title": title, "body": f"{title} 본문", "action": action,
        "class_no": "219", "class_header": class_header,
        "effective_date": effective_date, "notice_number": notice_number, "sequence": sequence,
        "source_name": "별지.hwpx", "source_sha256": "a" * 64, "role": "", "ordinal": 1,
    }


def document_record(sequence, effective_date, role, ordinal, *, notice_number="2026-42"):
    name = f"{role}-{ordinal}.hwp"
    return {
        "key": f"__{role}__{ordinal}", "title": name, "body": f"{role} 본문", "action": role,
        "class_no": "", "class_header": "",
        "effective_date": effective_date, "notice_number": notice_number, "sequence": sequence,
        "source_name": name, "source_sha256": f"{ordinal:064d}", "role": role, "ordinal": ordinal,
    }


def test_highlight_escapes_html_before_marking():
    rendered = search.highlight("<script>약제</script>", ["약제"])
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<mark>약제</mark>" in rendered


@pytest.mark.parametrize(
    ("action", "label"),
    [
        ("변경", "변경"),
        ("notice", "고시문"),
        ("comparison", "변경대비표"),
        ("reason", "개정이유"),
        ("", "기준"),
    ],
)
def test_action_labels_are_korean(action, label):
    assert search.action_label(action) == label


def test_static_index_is_deterministic_and_contains_provenance(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    public = tmp_path / "public"
    normalized.mkdir()
    document = normalized_document()
    (normalized / "seq-1.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "NORMALIZED", normalized)
    monkeypatch.setattr(build_site, "PUBLIC", public)

    build_site.main()
    first = (public / "search-index.json").read_bytes()
    build_site.main()
    second = (public / "search-index.json").read_bytes()

    assert first == second
    row = json.loads(first)[0]
    assert row["sequence"] == "seq-1"
    assert row["source_sha256"] == "a" * 64
    assert "다파글리플로진" in row["body"]
    page = (public / "index.html").read_text(encoding="utf-8")
    assert "notice:'고시문'" in page
    assert "관련 고시문 및 첨부자료" in page
    assert "효능·효과가 같은 품목은 함께 표시합니다." in page
    assert "source_sha256.slice" not in page
    assert "const list=el('ul')" not in page
    assert "개 품목 (${representative.item_name}" in page
    # 저장 순서가 현재 개정 우선이므로 클라이언트는 재정렬하지 않는다.
    assert "const revisions=doc.revisions||[];" in page
    assert ".slice().sort" not in page


def _search_db_document(digest: str = "c" * 64) -> dict:
    return {
        "version": {"시행일자": "20260401", "발령번호": "2026-92", "발령일자": "20260325",
                    "행정규칙일련번호": "9000000000001", "행정규칙명": "약제"},
        "attachments": [{
            "ordinal": 1, "source_url": "https://www.law.go.kr/file/1",
            "original_name": "별지.hwpx", "stored_name": "별지.hwpx", "format": "hwpx",
            "role": "annex", "size": 10, "sha256": digest, "status": "complete",
            "parser_version": "test", "parser_status": "complete",
        }],
        "entries": [{
            "action": "변경", "class_no": "219", "class_header": "[219] 기타",
            "title": "Dapagliflozin 경구제", "body": "급여 기준 본문",
            "attachment_ordinal": 1, "attachment_sha256": digest,
            "block_identity": "[219]dapagliflozin경구제",
        }],
    }


def test_query_matches_effective_date_and_notice_number(tmp_path, monkeypatch):
    import ingest

    database = tmp_path / "criteria.db"
    monkeypatch.setattr(ingest, "DB_PATH", database)
    ingest._rebuild_database([_search_db_document()])
    monkeypatch.setattr(search, "DB_PATH", database)

    for terms in (["2026-04-01"], ["20260401"], ["2026-92"], ["제2026-92호"], ["dapagliflozin"]):
        rows = search.query(terms)
        assert [row["title"] for row in rows] == ["Dapagliflozin 경구제"], terms
    assert search.query(["2026-05-01"]) == []
    # 본문 검색어와 날짜를 섞으면 어느 쪽이든 일치한 항목을 돌려준다(OR)
    rows = search.query(["없는약제", "2026-04-01"])
    assert [row["발령번호"] for row in rows] == ["2026-92"]


def test_split_terms_classifies_dates_and_notices():
    text, dates, notices = search.split_terms(
        ["다파글리플로진", "2026-04-01", "20260401", "제2026-92호", "2026-117"],
    )
    assert text == ["다파글리플로진"]
    assert dates == ["20260401", "20260401"]
    assert notices == ["2026-92", "2026-117"]


def test_query_reports_missing_database(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises(RuntimeError, match="검색 DB가 없습니다"):
        search.query(["test"])


def test_build_index_adds_role_and_ordinal(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    public = tmp_path / "public"
    normalized.mkdir()
    (normalized / "seq-1.json").write_text(json.dumps(normalized_document(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "NORMALIZED", normalized)
    monkeypatch.setattr(build_site, "PUBLIC", public)

    build_site.main()
    rows = json.loads((public / "search-index.json").read_text(encoding="utf-8"))
    by_key = {row["key"]: row for row in rows}
    criterion = by_key["[219]dapagliflozin경구제"]
    assert criterion["role"] == ""
    assert criterion["ordinal"] == 1
    notice = by_key["__notice__" + "b" * 64]
    assert notice["role"] == "notice"
    assert notice["ordinal"] == 2


def test_build_mfds_public_writes_search_and_detail_indexes(tmp_path, monkeypatch):
    source = tmp_path / "mfds" / "items"
    public = tmp_path / "public"
    source.mkdir(parents=True)
    item = {
        "schema_version": 1,
        "complete": True,
        "item_seq": "202600001",
        "item_name": "시험약",
        "entp_name": "시험제약",
        "permit_date": "20260101",
        "cancel_date": "",
        "status": "정상",
        "main_item_ingr": "Dapagliflozin",
        "main_item_ingr_eng": "Dapagliflozin",
        "edi_code": "",
        "atc_code": "A10BK01",
        "source_url": "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=202600001",
        "revisions": [{
            "revision_id": "202600001-" + "a" * 8,
            "content_sha256": "a" * 64,
            "ee_text": "제2형 당뇨병",
            "ee_doc_id": "EE-1",
            "first_observed_at": "2026-08-21T00:00:00Z",
            "last_observed_at": "2026-08-21T01:00:00Z",
        }],
    }
    (source / "202600001.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "MFDS_ITEMS", source)
    monkeypatch.setattr(build_site, "PUBLIC", public)

    index = build_site.build_mfds_public()

    assert index == [{
        "item_seq": "202600001",
        "item_name": "시험약",
        "entp_name": "시험제약",
        "main_item_ingr": "Dapagliflozin",
        "main_item_ingr_eng": "Dapagliflozin",
        "status": "정상",
        "permit_date": "20260101",
        "revision_count": 1,
        "current_content_sha256": "a" * 64,
        "last_observed_at": "2026-08-21T01:00:00Z",
        "source_url": item["source_url"],
    }]
    assert json.loads((public / "mfds" / "items" / "202600001.json").read_text(encoding="utf-8")) == item
    assert json.loads((public / "mfds" / "search-index.json").read_text(encoding="utf-8")) == index


def test_criterion_groups_and_items_are_newest_first():
    records = [
        criterion_record("a", "20240101", "2024-1", title="옛 제목"),
        criterion_record("a", "20260301", "2026-42", title="새 제목"),
        criterion_record("a", "20250101", "2025-1"),
        criterion_record("b", "20250701", "2025-73"),
    ]
    groups = search.group_criteria(records)

    assert [items[0]["key"] for items in groups] == ["a", "b"]
    assert [record["effective_date"] for record in groups[0]] == ["20260301", "20250101", "20240101"]


def test_criterion_ties_break_by_sequence_desc():
    records = [
        criterion_record("a", "20260301", "2026-10"),
        criterion_record("a", "20260301", "2026-42"),
        criterion_record("b", "20260301", "2026-42"),
        criterion_record("b", "20260301", "2026-99"),
    ]
    groups = search.group_criteria(records)

    assert [record["sequence"] for record in groups[0]] == ["2026-99", "2026-42"]
    assert [record["sequence"] for record in groups[1]] == ["2026-42", "2026-10"]
    assert [items[0]["sequence"] for items in groups] == ["2026-99", "2026-42"]


def test_criterion_heading_comes_from_newest_item():
    records = [
        criterion_record("a", "20240101", "2024-1", title="옛 제목", class_header="옛 머리글"),
        criterion_record("a", "20260301", "2026-42", title="새 제목", class_header="새 머리글"),
    ]
    (items,) = search.group_criteria(records)

    assert items[0]["title"] == "새 제목"
    assert items[0]["class_header"] == "새 머리글"


def test_documents_group_by_sequence_with_revision_header():
    records = [
        document_record("2025-73", "20250701", "notice", 1, notice_number="2025-73"),
        document_record("2026-42", "20260301", "reason", 2),
        document_record("2026-42", "20260301", "notice", 1),
    ]
    groups = search.group_documents(records)

    assert [items[0]["sequence"] for items in groups] == ["2026-42", "2025-73"]
    assert all({record["sequence"] for record in items} == {items[0]["sequence"]} for items in groups)
    head = groups[0][0]
    assert search.revision_header(head["effective_date"], head["notice_number"]) == "2026-03-01 시행 · 고시 제2026-42호"


def test_document_cards_order_by_role_rank_then_ordinal():
    records = [
        document_record("2026-42", "20260301", "other", 1),
        document_record("2026-42", "20260301", "notice", 3),
        document_record("2026-42", "20260301", "qa", 1),
        document_record("2026-42", "20260301", "reason", 2),
        document_record("2026-42", "20260301", "reason", 1),
        document_record("2026-42", "20260301", "comparison", 4),
    ]
    (items,) = search.group_documents(records)

    assert [(record["role"], record["ordinal"]) for record in items] == [
        ("notice", 3), ("reason", 1), ("reason", 2), ("comparison", 4), ("qa", 1), ("other", 1),
    ]
    assert [search.role_rank(role) for role in search.ROLE_ORDER] == list(range(len(search.ROLE_ORDER)))
    assert search.role_rank("unknown") > search.role_rank("other")


def test_static_page_has_footer_and_no_disclaimer(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    public = tmp_path / "public"
    normalized.mkdir()
    (normalized / "seq-1.json").write_text(json.dumps(normalized_document(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "NORMALIZED", normalized)
    monkeypatch.setattr(build_site, "PUBLIC", public)

    build_site.main()
    page = (public / "index.html").read_text(encoding="utf-8")
    assert '<a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">데이터 수집·검증 과정 보기</a>' in page
    assert "최근 갱신: 2024-12-31" in page
    assert "new URLSearchParams(location.search).get('q')" in page
    assert "https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq=" in page
    # 품명 검색은 정확→전방→부분문자열→성분 계층이며 부분수열 매칭은 없어야 한다
    assert "const mfdsTier=" in page
    assert "fuzzyContains" not in page
    # 정확 일치가 존재하면 그것만 반환한다
    assert "pairs=pairs.filter(pair=>pair[0]===0)" in page
    # 묶음 대표는 계층이 가장 좋은 품목이어야 한다
    assert "products.reduce((best,item)=>mfdsTier(item,terms||[])" in page
    # SEO: canonical·JSON-LD·크롤러 파일·정적 약제 목록
    assert '<link rel="canonical" href="https://rxcodelab.github.io/korea-drug-reimbursement-criteria/">' in page
    assert 'application/ld+json' in page and '"SearchAction"' in page
    assert "수록된 약제 급여기준" in page
    assert "Dapagliflozin 경구제" in page  # JS 없이 크롤러가 읽는 정적 목록
    robots = (public / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://rxcodelab.github.io/korea-drug-reimbursement-criteria/sitemap.xml" in robots
    sitemap = (public / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://rxcodelab.github.io/korea-drug-reimbursement-criteria/</loc>" in sitemap
    # 항목별 정적 페이지: 파일 생성, 목록 링크, sitemap 등재, 본문·원문 링크 포함
    pages = list((public / "criteria").glob("*.html"))
    assert len(pages) == 1
    detail = pages[0].read_text(encoding="utf-8")
    assert "Dapagliflozin 경구제 급여기준 변경 이력" in detail
    assert "고시 제2024-1호" in detail
    assert "admRulLsInfoP.do?admRulSeq=seq-1" in detail
    assert 'rel="canonical"' in detail
    assert f'href="criteria/{pages[0].name}"' in page.replace("dapagliflozin-%EA%B2%BD%EA%B5%AC%EC%A0%9C", pages[0].name.removesuffix(".html")) or "criteria/dapagliflozin" in page
    assert sitemap.count("<loc>") == 2
    assert "history.replaceState" in page
    # 시행일(2026-04-01·20260401)과 고시번호도 검색 대상에 포함된다
    assert "dateLabel(record.effective_date)+'\\n'+record.effective_date" in page
    assert "고시 제'+record.notice_number+'호" in page
    assert "비공식" not in page


def test_slugify_makes_stable_url_slugs():
    assert build_site.slugify("Dapagliflozin 경구제 (품명: 포시가정 10밀리그램)") == (
        "dapagliflozin-경구제-품명-포시가정-10밀리그램"
    )
    assert build_site.slugify("!!!") == "criteria"


def test_cli_report_orders_newest_first_with_footer():
    records = [
        criterion_record("a", "20240101", "2024-1", title="옛 제목"),
        criterion_record("a", "20260301", "2026-42", title="새 제목"),
        document_record("2025-73", "20250701", "notice", 1, notice_number="2025-73"),
        document_record("2026-42", "20260301", "reason", 2),
    ]
    report = search.build_html_report(["다파"], records)

    assert search.FOOTER_URL in report
    assert "데이터 수집·검증 과정 보기" in report
    assert "최근 갱신" not in report
    dated = search.build_html_report(["다파"], records, ("20260729", "2026-159"))
    assert "최근 갱신: 2026-07-29" in dated
    assert f'<form action="{search.SITE_URL}" method="get">' in report
    assert "최신 데이터에서 검색" in report
    assert search.notice_url("2026-42") in report
    assert "국가법령정보센터 원문 보기" in report
    assert "비공식" not in report
    assert report.index("새 제목") < report.index("옛 제목")
    assert "2026-03-01 시행 · 고시 제2026-42호" in report
    assert "관련 고시문 및 첨부자료 2건" in report
