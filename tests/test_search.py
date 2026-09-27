import collections
import json
import re

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
    assert "다파글리플로진" in row["body"]
    # Only fields the page reads; hashes stay in the SQLite snapshot.
    assert set(row) <= set(build_site.CRITERIA_INDEX_FIELDS) and "source_sha256" not in row
    page = (public / "index.html").read_text(encoding="utf-8")
    assert 'const actionLabels={"notice":"고시문"' in page
    assert '"notice":"고시문"' in page and '"comparison":"변경대비표"' in page
    assert 'const roleRanks={"notice":0' in page
    assert "관련 고시문 및 첨부자료" in page
    assert "효능·효과가 같은 품목은 함께 묶었습니다." in page
    assert "source_sha256.slice" not in page
    assert "const list=el('ul')" not in page
    assert "summary.append(shortProductName(representative.item_name" in page
    # Example searches, with the newest effective date from the data.
    assert 'data-q="다파글리플로진"' in page and 'data-q="2025-01-01"' in page
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
    criteria = json.loads((public / "search-index.json").read_text(encoding="utf-8"))
    documents = json.loads((public / "documents-index.json").read_text(encoding="utf-8"))
    assert [row["key"] for row in criteria] == ["[219]dapagliflozin경구제"]
    assert "role" not in criteria[0] and "ordinal" not in criteria[0]
    (notice,) = documents
    assert notice["role"] == "notice"
    assert notice["ordinal"] == 2
    assert set(notice) <= set(build_site.DOCUMENT_INDEX_FIELDS)


def test_has_clean_current_text_rejects_only_stale_markup_leftovers():
    # 파손이 없으면(정상) 게시 대상이다.
    assert build_site.has_clean_current_text({"revisions": [{"ee_text": "당뇨병"}]})
    assert build_site.has_clean_current_text({"revisions": [{"ee_text": ""}]})
    # revisions 자체가 없으면 게시 대상이 아니다.
    assert not build_site.has_clean_current_text({"revisions": []})
    assert not build_site.has_clean_current_text({})
    # 현행(revisions[0]) 텍스트에 예전 파싱 버그의 HTML 태그·미해제 엔티티가 남아 있으면 게시하지 않는다.
    assert not build_site.has_clean_current_text({"revisions": [{"ee_text": "위<sup>.</sup>십이지장굴양"}]})
    assert not build_site.has_clean_current_text({"revisions": [{"ee_text": "&nbsp;적응증"}]})
    # 과거 개정에 깨진 텍스트가 있어도 현행이 깨끗하면 게시 대상이다.
    assert build_site.has_clean_current_text({
        "revisions": [{"ee_text": "깨끗함"}, {"ee_text": "<sup>깨짐</sup>"}],
    })


def test_search_script_inlines_core_before_ui_with_no_placeholders_left():
    """검색 JS는 assets/의 두 파일에서 오고, core가 ui보다 먼저 실려야 한다.

    ui 첫 줄이 SearchCore를 구조 분해하므로 순서가 뒤집히면 페이지 전체가 죽는다.
    """
    script = build_site.search_script()
    assert script.index("const SearchCore=") < script.index("}=SearchCore;")
    assert "module.exports=SearchCore" in script  # Node 테스트가 잡을 수 있는 형태

    page = build_site.render_index_page([], "")
    assert "__SEARCH_JS__" not in page
    # 주입 플레이스홀더가 하나라도 남으면 문법 오류가 된다.
    assert not re.search(r"__[A-Z_]+__", page), "치환되지 않은 플레이스홀더가 남았습니다"
    assert "const SearchCore=" in page and "}=SearchCore;" in page


def test_encode_mfds_index_round_trips_every_row():
    """Decoding the columns gives back every row; the browser relies on the same rules."""
    rows = [
        ["1", "가정", "제약갑", "성분A", "IngredientA", "20200101", 2, "a" * 64, ""],
        ["2", "나정", "제약갑", "성분A", "IngredientA", "20200101", 1, "b" * 64, "2024-04-25 취하"],
        ["3", "다정", "제약을", "성분B", "", "20210202", 3, "a" * 64, ""],
    ]
    written = build_site.encode_mfds_index(rows)

    dicts = written["dicts"]
    fields = build_site.MFDS_INDEX_FIELDS
    decode = lambda field, r: dicts[field][written[field][r]] if field in dicts else written[field][r]
    restored = [[str(written["item_seq"][r]), *(decode(field, r) for field in fields[1:7]), decode("withdrawn", r)]
                for r in range(len(rows))]
    assert restored == [row[:7] + row[8:] for row in rows]
    # Same indication hash, same group id; hashes themselves are not shipped.
    assert written["content_group"] == [0, 1, 0]
    assert written["item_seq"] == [1, 2, 3]
    assert dicts["entp_name"] == ["제약갑", "제약을"]
    assert dicts["withdrawn"] == ["", "2024-04-25 취하"]


def test_encode_mfds_index_handles_empty_rows():
    written = build_site.encode_mfds_index([])
    assert written["item_name"] == [] and written["content_group"] == []
    assert written["dicts"] == {field: [] for field in build_site.MFDS_INDEX_DICT_FIELDS}


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
            "normalizer_version": 3,
            "first_observed_at": "2026-08-21T00:00:00Z",
            "last_observed_at": "2026-08-21T01:00:00Z",
        }],
    }
    # normalizer_version이 없는(옛 검색어 API 시절 수집, 아직 재수집 안 됨) 품목도 게시 대상이다 —
    # 급여기준과의 연결 여부는 더 이상 게시 조건이 아니다. ee_text가 깨끗하면(태그·엔티티 잔존 없음) 실린다.
    unrematched_item = {**item, "item_seq": "202600002", "item_name": "무관 품목"}
    unrematched_item["revisions"] = [{
        "revision_id": "202600002-" + "b" * 8,
        "content_sha256": "b" * 64,
        "ee_text": "무관 품목",
        "ee_doc_id": "EE-2",
        "first_observed_at": "2026-08-21T00:00:00Z",
        "last_observed_at": "2026-08-21T01:00:00Z",
    }]
    # 옛 파싱 버그로 본문에 HTML 태그가 그대로 남은 레코드는 재수집 전까지 게시하지 않는다.
    broken_item = {**item, "item_seq": "202600003", "item_name": "깨진 품목"}
    broken_item["revisions"] = [{
        "revision_id": "202600003-" + "c" * 8,
        "content_sha256": "c" * 64,
        "ee_text": "위<sup>.</sup>십이지장궤양",
        "ee_doc_id": "EE-3",
        "first_observed_at": "2026-08-21T00:00:00Z",
        "last_observed_at": "2026-08-21T01:00:00Z",
    }]
    (source / "202600001.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    (source / "202600002.json").write_text(json.dumps(unrematched_item, ensure_ascii=False), encoding="utf-8")
    (source / "202600003.json").write_text(json.dumps(broken_item, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "MFDS_ITEMS", source)
    monkeypatch.setattr(build_site, "PUBLIC", public)

    index, build = build_site.build_mfds_public()

    assert index == [
        ["202600002", "무관 품목", "시험제약", "Dapagliflozin", "Dapagliflozin", "20260101", 1, "b" * 64, ""],
        ["202600001", "시험약", "시험제약", "Dapagliflozin", "Dapagliflozin", "20260101", 1, "a" * 64, ""],
    ]
    written = json.loads((public / "mfds" / "search-index.json").read_text(encoding="utf-8"))
    encoded = build_site.encode_mfds_index(index)
    assert written == {**encoded, "build": build} and build == build_site.mfds_build_id(encoded)
    assert set(written) == {"item_seq", "item_name", "revision_count", "content_group", "dicts", "build",
                            *build_site.MFDS_INDEX_DICT_FIELDS}
    # Both rows share manufacturer and ingredient, so each dictionary has one entry.
    assert written["dicts"]["entp_name"] == ["시험제약"] and written["entp_name"] == [0, 0]
    assert written["content_group"] == [0, 1]
    assert json.loads((public / "mfds" / "items" / "202600001.json").read_text(encoding="utf-8")) == item
    assert json.loads((public / "mfds" / "items" / "202600002.json").read_text(encoding="utf-8")) == unrematched_item
    # 옛 파싱 버그로 본문이 깨진 레코드는 재수집되기 전까지 색인과 상세 어느 쪽에도 실리지 않는다.
    assert not (public / "mfds" / "items" / "202600003.json").exists()


def test_mfds_build_id_changes_when_shard_layout_changes(monkeypatch):
    encoded = build_site.encode_mfds_index([
        ["1", "약", "제약", "성분", "Ingredient", "20200101", 1, "a" * 64, ""],
    ])
    original = build_site.mfds_build_id(encoded)
    monkeypatch.setattr(build_site, "MFDS_BLOCK_ROWS", build_site.MFDS_BLOCK_ROWS + 1)
    assert build_site.mfds_build_id(encoded) != original
    monkeypatch.setattr(build_site, "MFDS_GRAM_BUCKETS", build_site.MFDS_GRAM_BUCKETS + 1)
    assert build_site.mfds_build_id(encoded) != original


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
    assert "const mfdsTier=" not in page  # test-only reference, kept out of the page
    assert "fuzzyContains" not in page
    # 정확 일치는 순위만 올린다. 다른 검색어의 결과를 지우면 안내한 OR 검색 계약과 어긋난다.
    assert "matches.filter(match=>match[0]===0)" not in page
    assert "if(tier<4)matches.push([tier,r])" in page and "matches.sort((a,b)=>a[0]-b[0])" in page
    # Product data loads on demand; search keys are computed once at load; input is debounced.
    # The full product index is fetched only by fullIndex(); searches use fragment files first.
    assert "mfds/search-index.json" in page.split("function fullIndex()")[1].split("function productsFor")[0]
    assert page.count("mfds/search-index.json") == 1
    # Every product file is checked against the page's build.
    assert 'const mfdsBuild="' in page and "file.build!==mfdsBuild" in page
    # Documents load last, and never while the MFDS index is downloading.
    assert "data.mfds.state==='loading')return;" in page.split("function ensureDocuments()")[1]
    assert "raw.map(searchableCriterion)" in page and "record.hay.includes(term)" in page
    assert "setTimeout(render,150)" in page
    # Each dataset reports its own failure.
    assert "급여기준 색인을 불러오지 못했습니다" in page and "허가 품목을 불러오지 못했습니다" in page and "사이트가 갱신되었습니다" in page
    # 상세 페이지 URL은 최신 제목이 아니라 항목 식별자에서 나온다
    assert 'href="criteria/219-dapagliflozin%EA%B2%BD%EA%B5%AC%EC%A0%9C.html"' in page
    # A group's representative is its best-tier product.
    assert "groupRepresentative(index,bucket)" in page
    # No product objects at load time; only drawn groups call materializeMfds.
    assert "function buildMfdsIndex(" in page and "function materializeMfds(" in page
    # Only the representative and history products of a drawn group become objects.
    assert "materializeMfds(index,best[1],best[0])" in page and "bucket.map(match=>materializeMfds" not in page
    # SEO: canonical·JSON-LD·크롤러 파일·정적 약제 목록
    assert '<link rel="canonical" href="https://rxcodelab.github.io/korea-drug-reimbursement-criteria/">' in page
    assert 'application/ld+json' in page and '"Dataset"' in page and '"SearchAction"' not in page
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
    assert pages[0].name == "219-dapagliflozin경구제.html"
    assert sitemap.count("<loc>") == 2
    assert "<lastmod>2024-12-31</lastmod>" in sitemap
    assert '"BreadcrumbList"' in detail and '"dateModified":"2024-12-31"' in detail
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


def test_cli_query_follows_web_substring_contract(tmp_path, monkeypatch):
    """CLI는 웹과 같은 규칙(대소문자 무시 부분문자열, 같은 필드, 검색어 간 OR)이어야 한다.

    FTS5 단어 검색은 'dapa' 같은 앞부분, '다파글리플로진을' 같은 조사 붙은 한글, 분류 헤더를 놓쳤다.
    """
    import ingest

    document = _search_db_document()
    document["entries"][0]["body"] = "다파글리플로진을 투여할 때 급여 기준 본문"
    database = tmp_path / "criteria.db"
    monkeypatch.setattr(ingest, "DB_PATH", database)
    ingest._rebuild_database([document])
    monkeypatch.setattr(search, "DB_PATH", database)

    for terms in (["dapa"], ["DAPAGLIFLOZIN"], ["다파글리플로진"], ["[219] 기타"], ["고시 제2026-92호"]):
        assert [row["title"] for row in search.query(terms)] == ["Dapagliflozin 경구제"], terms
    assert search.query(["없는말"]) == []
    assert search.query([""]) == []
    assert len(search.query(["없는말", "dapa"])) == 1


def test_cli_haystack_matches_browser_search_key():
    """파이썬 haystack과 브라우저 searchableCriterion은 같은 필드를 같은 순서·구분자로 잇는다."""
    page = build_site.render_index_page([], "")
    # The browser also appends Korean name aliases, which the CLI database does not have.
    assert ("hay:(record.title+'\\n'+record.body+'\\n'+(record.class_header||'')+'\\n'"
            "+dateLabel(record.effective_date)+'\\n'+record.effective_date+'\\n고시 제'+record.notice_number+'호'"
            "+(record.aliases?'\\n'+record.aliases:'')).toLowerCase()") in page
    record = criterion_record("a", "20260401", "2026-92", title="Dapagliflozin 경구제", class_header="[219] 기타",
                              notice_number="2026-92")
    assert search.haystack(record) == "\n".join([
        "Dapagliflozin 경구제", "Dapagliflozin 경구제 본문", "[219] 기타", "2026-04-01", "20260401", "고시 제2026-92호",
    ])
    assert search.matches(record, ["없는말", "DAPA"]) and not search.matches(record, ["없는말"])


def test_last_success_label_reads_state_file_and_omits_failure_detail(tmp_path, monkeypatch):
    """푸터는 마지막 수집 성공 날짜만 보여야 하고, 시도·실패 정보는 절대 담지 않아야 한다."""
    state = tmp_path / "collection.json"
    monkeypatch.setattr(build_site, "COLLECTION_STATUS_PATH", state)

    assert build_site.last_success_label() == ""  # 파일이 없으면(최초 실행) 조용히 생략된다

    state.write_text(json.dumps({"last_success_date": "20260906"}), encoding="utf-8")
    label = build_site.last_success_label()
    assert label == "마지막 수집 성공: 2026-09-06 · "
    assert "실패" not in label and "시도" not in label and "warn" not in label

    state.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        build_site.last_success_label()


def test_index_footer_shows_last_success_date_without_failure_markup(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    (normalized / "seq-1.json").write_text(json.dumps(normalized_document(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "NORMALIZED", normalized)
    monkeypatch.setattr(build_site, "PUBLIC", tmp_path / "public")
    monkeypatch.setattr(build_site, "COLLECTION_STATUS_PATH", tmp_path / "collection.json")
    build_site.main()

    def footer(content: str) -> str:
        return content.split("<footer>", 1)[1].split("</footer>", 1)[0]

    page = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    detail = next((tmp_path / "public" / "criteria").glob("*.html")).read_text(encoding="utf-8")
    for content in (page, detail):
        section = footer(content)
        assert "최근 갱신: 2024-12-31" in section
        assert "실패" not in section and "시도" not in section and "warn" not in section

    (tmp_path / "collection.json").write_text(json.dumps({"last_success_date": "20260906"}), encoding="utf-8")
    build_site.main()
    page = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    detail = next((tmp_path / "public" / "criteria").glob("*.html")).read_text(encoding="utf-8")
    for content in (page, detail):
        section = footer(content)
        assert "최근 갱신: 2024-12-31" in section
        assert "마지막 수집 성공: 2026-09-06" in section
        assert "실패" not in section and "시도" not in section and "warn" not in section


def test_identity_slug_is_stable_across_title_changes(tmp_path, monkeypatch):
    """같은 항목의 최신 품명이 바뀌어도 상세 페이지 URL은 그대로여야 한다(기존 URL 404 방지)."""
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    first = normalized_document()
    (normalized / "seq-1.json").write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "NORMALIZED", normalized)
    monkeypatch.setattr(build_site, "PUBLIC", tmp_path / "public")
    build_site.main()
    before = sorted(p.name for p in (tmp_path / "public" / "criteria").glob("*.html"))

    second = normalized_document()
    second["version"] = {**second["version"], "시행일자": "20260301", "발령번호": "2026-42", "행정규칙일련번호": "seq-2"}
    second["entries"][0]["title"] = "Dapagliflozin 경구제 (품명: 포시가정 10밀리그램 등)"
    (normalized / "seq-2.json").write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    build_site.main()
    after = sorted(p.name for p in (tmp_path / "public" / "criteria").glob("*.html"))

    assert before == after == ["219-dapagliflozin경구제.html"]
    assert build_site.identity_slug("[일반원칙]고가의약품") == "일반원칙-고가의약품"


def test_colliding_slugs_get_order_independent_suffixes():
    """Ⅷ/Ⅸ처럼 슬러그가 겹치는 식별자는 정렬 순서와 무관하게 각자 고정 URL을 가져야 한다."""
    a = "[339]recombinantbloodcoagulationfactorⅷ-fcfusionproteinefmoroctocogα주사제"
    b = "[339]recombinantbloodcoagulationfactorⅸ-fcfusionproteineftrenonacogα주사제"
    # NFKC가 Ⅷ→viii, Ⅸ→ix로 풀어 슬러그가 애초에 다르다
    assert build_site.identity_slug(a) != build_site.identity_slug(b)
    assert build_site.identity_slug(a).startswith("339-recombinantbloodcoagulationfactorviii")
    # 그래도 겹치는 경우(예: 특수문자만 다른 식별자)는 양쪽 모두 해시 접미어를 받는다
    c, d = "[100]drug+a", "[100]drug-a"
    assert build_site.identity_slug(c) == build_site.identity_slug(d)
    forward = build_site.page_slugs([c, d])
    backward = build_site.page_slugs([d, c])
    assert forward == backward
    assert forward[c] != forward[d] and all(slug.startswith("100-drug-a-") for slug in forward.values())
    assert build_site.page_slugs([c]) == {c: "100-drug-a"}


def test_korean_names_prefer_shared_prefix_and_originator(monkeypatch):
    assert build_site.ingredient_parts("[M1]메글루민|[M1]메글루민|[M2]도타") == ["메글루민", "도타"]
    assert build_site.brand_name("포시가정10밀리그램(다파글리플로진프로판디올수화물)") == "포시가정"
    labels = collections.Counter({"다파글리플로진프로판디올수화물": 5, "다파글리플로진": 1, "다파글리플로진 + 메트포르민": 3})
    assert build_site.korean_ingredient(labels) == "다파글리플로진"
    assert build_site.korean_ingredient(collections.Counter({"암피실린나트륨 + 설박탐나트륨": 2})) == "암피실린나트륨 + 설박탐나트륨"

    product = lambda seq, name, ingr, permit, status="정상", cancel="": {
        "item_seq": seq, "item_name": name, "main_item_ingr": ingr, "main_item_ingr_eng": "Dapagliflozin",
        "permit_date": permit, "status": status, "cancel_date": cancel}
    products = [
        product("3", "다파진정10밀리그램", "[M1]다파글리플로진프로판디올수화물", "20230101"),
        product("1", "포시가정10밀리그램(다파글리플로진프로판디올수화물)", "[M1]다파글리플로진프로판디올수화물", "20131126", "취하", "20240425"),
        product("2", "포시가정5밀리그램(다파글리플로진프로판디올수화물)", "[M1]다파글리플로진프로판디올수화물", "20131126", "취하", "20240425"),
        product("4", "무관정", "[M9]무관", "20200101"),
        product("7", "만료정10밀리그램", "[M1]다파글리플로진프로판디올수화물", "20200101", "유효기간만료", "20250101"),
    ]
    products[3]["main_item_ingr_eng"] = "Unrelated"
    # 만료정 is neither the originator nor marketed, so it is not listed.
    groups = [[{"identity": "k", "title": "Dapagliflozin 경구제 (품명: 다파엔정 등)"}]]
    names = build_site.criteria_names(groups, products)["k"]
    assert names == {"ingredient": "다파글리플로진", "brands": [("포시가정", "2024-04-25 허가 취하"), ("다파진정", "")]}
    assert build_site.withdrawal_label({"status": "유효기간만료", "cancel_date": "20250522"}) == "2025-05-22 허가 유효기간만료"
    assert build_site.withdrawal_label({"status": "폐업", "cancel_date": "20230927"}) == "2023-09-27 업체 폐업"
    assert build_site.withdrawal_label({"status": "정상", "cancel_date": ""}) == ""
    assert build_site.name_aliases(names) == "다파글리플로진\n포시가정, 다파진정"
    assert build_site.base_ingredient("리오시구앗(미분화)") == "리오시구앗"
    assert build_site.base_ingredient("펙수프라잔염산염") == "펙수프라잔"
    assert build_site.base_ingredient("발프로산") == "발프로산"
    assert build_site.base_ingredient("미분화부데소니드") == "부데소니드"
    assert build_site.base_ingredient("오데빅시바트1.5수화물") == "오데빅시바트"
    # The notice's own title comes first; the 식약처 name follows in parentheses.
    assert build_site.page_heading("Dapagliflozin 경구제", {**names, "ingredient": "다파글리플로진"}) == (
        "Dapagliflozin 경구제 급여기준 변경 이력 (다파글리플로진)")
    # Long or missing Korean names are left out.
    assert build_site.page_heading("Agalsidase β 35mg 주사제", {**names, "ingredient": "아갈시다제베타(재조합인간알파갈락토시다제A)"}) == (
        "Agalsidase β 35mg 주사제 급여기준 변경 이력")
    assert build_site.page_heading("Dapagliflozin 경구제", None) == "Dapagliflozin 경구제 급여기준 변경 이력"
    # Injection criteria list injection products only.
    injection = [{**products[0], "item_seq": "5", "item_name": "다파주"}, {**products[0], "item_seq": "6", "item_name": "다파정"}]
    only = build_site.criteria_names([[{"identity": "i", "title": "Dapagliflozin 주사제"}]], injection)["i"]
    assert only["brands"] == [("다파주", "")]


def test_page_receives_product_suffixes_longest_first():
    """productBase in assets/search-core.js takes the first suffix that fits, so longer ones must come first."""
    lengths = [len(suffix) for suffix in build_site.PRODUCT_STRIPPED]
    assert lengths == sorted(lengths, reverse=True)
    assert {"프로판디올수화물", "오수화물", "염산염"} <= set(build_site.PRODUCT_STRIPPED)
    page = build_site.render_index_page([], "")
    injected = page.split("const productSuffixes=", 1)[1].split(";", 1)[0]
    assert json.loads(injected) == list(build_site.PRODUCT_STRIPPED)


def test_criteria_page_links_general_principle_and_siblings(tmp_path, monkeypatch):
    public = tmp_path / "public"
    monkeypatch.setattr(build_site, "PUBLIC", public)
    document = normalized_document()
    drug, notice = document["entries"]
    drug.update(class_no="396", class_header="[396] 당뇨병용제", block_identity="[396]dapagliflozin경구제",
                body="[일반원칙] 당뇨병용제 “세부사항” 범위 내")
    sibling = {**drug, "title": "Empagliflozin 경구제", "block_identity": "[396]empagliflozin경구제", "body": "본문"}
    general = {**drug, "class_no": "일반원칙", "class_header": "[일반원칙] 당뇨병용제", "title": "당뇨병용제",
               "block_identity": "[일반원칙]당뇨병용제", "body": "일반원칙 본문"}
    document["entries"] = [drug, sibling, general, notice]

    catalog = build_site.build_criteria_pages(build_site.criteria_groups([document]), "")

    page = (public / "criteria" / "396-dapagliflozin경구제.html").read_text(encoding="utf-8")
    assert '함께 적용되는 일반원칙: <a href="%EC%9D%BC%EB%B0%98%EC%9B%90%EC%B9%99-%EB%8B%B9%EB%87%A8%EB%B3%91%EC%9A%A9%EC%A0%9C.html">당뇨병용제</a>' in page
    assert "같은 분류의 급여기준" in page and "Empagliflozin 경구제" in page
    listing = build_site.static_drug_list(catalog)
    # General principles come first, then classes by number.
    assert "<h3>일반원칙</h3>" in listing and listing.index("<h3>일반원칙</h3>") < listing.index("[396] 당뇨병용제")


def test_site_files_are_copied_except_readme(tmp_path, monkeypatch):
    site = tmp_path / "site"
    public = tmp_path / "public"
    site.mkdir()
    public.mkdir()
    (site / "README.txt").write_text("notes", encoding="utf-8")
    (site / "naverabc.html").write_text("naver-site-verification: naverabc.html", encoding="utf-8")
    monkeypatch.setattr(build_site, "SITE_FILES", site)
    monkeypatch.setattr(build_site, "PUBLIC", public)
    build_site.copy_static_files()
    assert (public / "naverabc.html").is_file() and not (public / "README.txt").exists()


def test_korean_ingredient_handles_spelling_salts_and_forms():
    Counter = collections.Counter
    # Spelling variants: the majority name wins instead of their shared prefix (was '클래리').
    assert build_site.korean_ingredient(Counter({"클래리트로마이신": 171, "클래리트로마이신제피과립": 41,
                                                 "클래리스로마이신제피과립": 1}), "Clarithromycin") == "클래리트로마이신"
    assert build_site.korean_ingredient(Counter({"사이클로스포린": 33, "시클로스포린": 4}), "Cyclosporine") == "사이클로스포린"
    # A salt named in the English title stays (was '디메틸').
    assert build_site.korean_ingredient(Counter({"디메틸푸마르산염(미분화)": 2, "디메틸푸마르산염": 2}),
                                        "Dimethyl fumarate") == "디메틸푸마르산염"
    # A shorter base covers longer products that start with it.
    assert build_site.korean_ingredient(Counter({"아데노신트리포스페이트이나트륨삼수화물": 11, "아데노신": 2}),
                                        "Adenosine") == "아데노신"
    assert build_site.korean_ingredient(Counter({"란소프라졸과립": 2}), "Lansoprazole") == "란소프라졸"
    # Products without a recorded ingredient do not blank the name.
    assert build_site.korean_ingredient(Counter({"": 1, "아스피린": 5}), "Aspirin") == "아스피린"
    assert build_site.korean_ingredient(Counter({"": 3}), "Aspirin") == ""


def test_criteria_listing_several_drugs_get_no_korean_name():
    item = {"item_seq": "1", "item_name": "란스톤캡슐", "main_item_ingr": "[M1]란소프라졸과립",
            "main_item_ingr_eng": "Lansoprazole", "permit_date": "20200101", "status": "정상"}
    title = "프로톤 펌프 억제 경구제 Omeprazole(품명: 유한로섹캡슐 등), Lansoprazole(품명: 란스톤캡슐 등)"
    assert build_site.criteria_names([[{"identity": "k", "title": title}]], [item]) == {}


def test_unique_titles_fall_back_to_the_full_notice_title():
    group = lambda identity, title: [{"identity": identity, "title": title, "class_no": "395"}]
    groups = [group("a", "Agalsidase β 35mg 주사제 (품명: 파브라자임주)"), group("b", "Cyclosporine 경구제 (품명: 사이폴엔)"),
              group("c", "Cyclosporine 경구제 (품명: 산디문)")]
    names = {"a": {"ingredient": "아갈시다제베타", "brands": []}}
    titles = build_site.unique_titles(groups, names)
    assert titles == {"a": "Agalsidase β 35mg 주사제 급여기준 변경 이력 (아갈시다제베타)",
                      "b": "Cyclosporine 경구제 (품명: 사이폴엔) 급여기준 변경 이력",
                      "c": "Cyclosporine 경구제 (품명: 산디문) 급여기준 변경 이력"}


def test_catalog_groups_by_class_number_across_header_wordings():
    entry = lambda title, header: {"title": title, "path": f"criteria/{title}.html", "class_no": "142", "class_header": header}
    listing = build_site.static_drug_list([entry("A", "[142] 자격요법제"), entry("B", "[142] 자격료법제"),
                                           entry("C", "[142] 자격요법제")])
    assert listing.count("<h3>") == 1 and "<h3>[142] 자격요법제</h3>" in listing


def test_mfds_shards_cover_every_row_and_fragment(tmp_path):
    rows = [
        ["3", "다파진정", "시험제약", "다파글리플로진", "Dapagliflozin", "20200101", 1, "a" * 64, ""],
        ["1", "가정", "시험제약", "메트포르민", "Metformin", "20210101", 2, "b" * 64, "2024-04-25 취하"],
        ["2", "나정", "다른제약", "다파글리플로진|메트포르민", "Dapagliflozin/Metformin", "20220101", 1, "a" * 64, ""],
    ]
    encoded = build_site.encode_mfds_index(rows)
    build = build_site.mfds_build_id(encoded)
    build_site.write_mfds_shards(tmp_path, rows, encoded, build)

    blocks = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((tmp_path / "blocks").glob("*.json"))]
    assert sorted(r for block in blocks for r in block["row"]) == [0, 1, 2]  # each row in exactly one block
    assert all(block["build"] == build for block in blocks)
    # Blocks cluster rows by ingredient; values are spelled out, not dictionary ids.
    assert blocks[0]["main_item_ingr"] == ["다파글리플로진", "다파글리플로진|메트포르민", "메트포르민"]
    grams = {}
    for path in (tmp_path / "grams").glob("*.json"):
        file = json.loads(path.read_text(encoding="utf-8"))
        assert file["build"] == build
        grams.update(file["grams"])
    assert len(list((tmp_path / "grams").glob("*.json"))) == build_site.MFDS_GRAM_BUCKETS
    for text in ("다파", "파글", "metf", "시험"):
        assert all(text[i:i + 2] in grams for i in range(len(text) - 1))
    # Fragments with punctuation or spaces are not indexed.
    assert all(gram.isalnum() for gram in grams) and "a/" not in grams
    assert blocks[0]["withdrawn"] == ["", "", "2024-04-25 취하"]
    # Same value as gramBucket in assets/search-core.js (tests/search-core.test.js).
    assert build_site.gram_bucket("다파") == 40


def test_product_link_searches_names_without_punctuation():
    newest = {"title": "Tiotropium 흡입제 (품명: 스피리바)", "class_no": "229", "class_header": "[229] 기타",
              "effective_date": "20250101", "notice_date": "20241231", "notice_number": "2024-1", "sequence": "s",
              "action": "변경", "body": "본문"}
    names = {"ingredient": "티오트로퓸 · 브롬화티오트로피움", "brands": []}
    page = build_site.criteria_page(newest, [newest], "criteria/x.html", names)
    assert 'href="../?q=%ED%8B%B0%EC%98%A4%ED%8A%B8%EB%A1%9C%ED%93%B8"' in page  # '티오트로퓸' only: one phrase
    assert "식약처 허가 성분명: 티오트로퓸 · 브롬화티오트로피움" in page
    combo = {**newest, "title": "Nirmatrelvir+ Ritonavir 경구제 (품명: 팍스로비드정)"}
    page = build_site.criteria_page(combo, [combo], "criteria/y.html", {"ingredient": "니르마트렐비르 + 리토나비르", "brands": []})
    assert 'href="../?q=Nirmatrelvir"' in page
