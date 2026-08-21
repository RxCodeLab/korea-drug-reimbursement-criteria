import json

import pytest

import build_site
import search


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
        "status": "정상",
        "permit_date": "20260101",
        "revision_count": 1,
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
    assert '<a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">GitHub 저장소</a>' in page
    assert "데이터 자동 갱신" in page
    assert "비공식" not in page


def test_cli_report_orders_newest_first_with_footer():
    records = [
        criterion_record("a", "20240101", "2024-1", title="옛 제목"),
        criterion_record("a", "20260301", "2026-42", title="새 제목"),
        document_record("2025-73", "20250701", "notice", 1, notice_number="2025-73"),
        document_record("2026-42", "20260301", "reason", 2),
    ]
    report = search.build_html_report(["다파"], records)

    assert search.FOOTER_URL in report
    assert "GitHub 저장소" in report
    assert "데이터 자동 갱신" in report
    assert "비공식" not in report
    assert report.index("새 제목") < report.index("옛 제목")
    assert "2026-03-01 시행 · 고시 제2026-42호" in report
    assert "관련 고시문 및 첨부자료 2건" in report
