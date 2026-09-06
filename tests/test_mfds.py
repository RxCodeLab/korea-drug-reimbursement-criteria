import contextlib
import hashlib
import io
import json

import pytest

import fetch_mfds
import verify

SERVICE_KEY = "TEST-SERVICE-KEY/abc=="


class FakeApi:
    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def __call__(self, url, params=None, retries=3):
        self.calls.append((url, dict(params or {})))
        return self.responder(params or {})


def api_item(seq, ee="<p>이 약은</p>\n두통에 사용", **overrides):
    item = {
        "ITEM_SEQ": str(seq),
        "ITEM_NAME": f"제품{seq}",
        "ENTP_NAME": "제약사",
        "ITEM_PERMIT_DATE": "20200101",
        "CANCEL_DATE": "",
        "CANCEL_NAME": "정상",
        "MAIN_ITEM_INGR": "성분",
        "MAIN_INGR_ENG": "Clonazepam",  # mfds_match가 이 필드로 색인한다. 기본값은 대부분의 테스트 검색어(Clonazepam)와 맞추었다.
        "EDI_CODE": "642101470",
        "ATC_CODE": "N02BA01",
        "EE_DOC_ID": f"EE-{seq}",
        "EE_DOC_DATA": ee,
    }
    item.update(overrides)
    return item


def envelope(body):
    return json.dumps({
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": body,
        },
    }, ensure_ascii=False).encode("utf-8")


def write_normalized(directory, titles):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "version.json").write_text(
        json.dumps({"entries": [{"title": title} for title in titles]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return directory


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_mfds, "REQUEST_SLEEP", 0)
    monkeypatch.setattr(fetch_mfds, "SYNC_PATH", tmp_path / "sync-state.json")
    monkeypatch.setattr(fetch_mfds, "BACKFILL_PATH", tmp_path / "backfill-state.json")


def test_normalize_ee_strips_tags_unescapes_and_collapses():
    raw = "<p>이 약은 &amp; 그람양성</p>\n<br/>균에 \t 작용합니다 .&nbsp;"
    assert fetch_mfds.normalize_ee(raw) == "이 약은 & 그람양성 균에 작용합니다 ."
    assert fetch_mfds.normalize_ee(None) == ""


def test_content_hash_is_sha256_of_normalized_text():
    text = fetch_mfds.normalize_ee("<p>두통</p>")
    assert fetch_mfds.content_sha256(text) == hashlib.sha256("두통".encode("utf-8")).hexdigest()


def history_page(*rows: str) -> bytes:
    """의약품안전나라 변경이력 페이지의 실제 구조(hist_list 표 안의 detailHist 링크)."""
    body = "".join(f'<tr><td class="pc-tr"><span>★</span></td><td>{row}</td></tr>' for row in rows)
    return (
        '<div id="list"><h2 class="title-t">변경이력</h2>'
        '<table id="hist_list" class="tb_list type2"><thead><tr><th>선택</th><th>순번</th><th>변경일자</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    ).encode("utf-8")


def history_link(doc_id: str, date: str, cdata: str) -> str:
    return (
        f'<a href="#" data-docdata="&lt;DOC title=&quot;효능효과&quot;&gt;&lt;PARAGRAPH&gt;&lt;![CDATA[{cdata}]]&gt;'
        f'&lt;/PARAGRAPH&gt;&lt;/DOC&gt;" onclick="detailHist(&#39;{doc_id}&#39;, &#39;{date}&#39;, this); return false;">'
        f"<span>{date}</span></a>"
    )


def test_parse_history_extracts_official_revision():
    source = history_page(history_link("52", "2023-06-16", "과거 적응증"))

    (revision,) = fetch_mfds.parse_history(source, "201310308", "2026-08-21T00:00:00Z")

    assert revision["ee_text"] == "과거 적응증"
    assert revision["ee_doc_id"] == "52"
    assert revision["official_revision_date"] == "2023-06-16"


def test_parse_history_keeps_clinical_inequalities_inside_cdata():
    """`<[^>]+>` 태그 제거를 XML 파싱 뒤에 또 걸면 'CrCl < 30 또는 CrCl > 60'이 'CrCl 60'이 된다."""
    source = history_page(history_link("7", "2024-01-01", "CrCl &lt; 30 mL/min 또는 CrCl &gt; 60 mL/min인 환자"))
    (revision,) = fetch_mfds.parse_history(source, "1", "2026-08-21T00:00:00Z")
    assert revision["ee_text"] == "CrCl < 30 mL/min 또는 CrCl > 60 mL/min인 환자"


def test_normalize_ee_keeps_inequalities_in_xml_and_html():
    xml = "<DOC><PARAGRAPH><![CDATA[CrCl < 30 또는 CrCl > 60]]></PARAGRAPH></DOC>"
    assert fetch_mfds.normalize_ee(xml) == "CrCl < 30 또는 CrCl > 60"
    assert fetch_mfds.normalize_ee("<p>CrCl &lt; 30 또는 &gt; 60</p><p>두통</p>") == "CrCl < 30 또는 > 60 두통"


@pytest.mark.parametrize("page", [b"<html></html>", "<html><body>점검 중입니다</body></html>".encode("utf-8"), b""])
def test_parse_history_rejects_pages_without_history_table(page):
    """차단·점검 페이지나 바뀜 마크업을 '이력 없음'으로 취급하면 history_fetched_at이 기록돼 영원히 재시도하지 않는다."""
    with pytest.raises(RuntimeError, match="변경이력 표 없음"):
        fetch_mfds.parse_history(page, "1", "2026-08-21T00:00:00Z")


def test_parse_history_accepts_explicit_empty_table():
    assert fetch_mfds.parse_history(history_page(), "1", "2026-08-21T00:00:00Z") == []


def test_search_terms_drop_class_words_filenames_and_duplicates(tmp_path):
    titles = [
        "Clonazepam 경구제 (품명: 리보트릴정 등)",
        "clonazepam 경구제 (품명: 리보트릴정)",
        "[일반원칙] 향정신성약물",
        "당뇨병 용제",
        "고시개정문 1부(약제).hwp",
        "Quetiapine fumarate 경구제 (품명∶쎄로켈정 등)",
        "Fluvoxamine maleate (품명: 듀미록스정), Imipramine HCl (품명: 이미프라민정 등)",
    ]
    groups = fetch_mfds.term_groups_from_titles(fetch_mfds.load_titles(write_normalized(tmp_path, titles)))
    assert groups == [
        ("Clonazepam", ["리보트릴정"]),
        ("Quetiapine fumarate", ["쎄로켈정"]),
        ("Fluvoxamine maleate", ["듀미록스정", "이미프라민정"]),
    ]


def test_universe_request_carries_service_key_and_no_search_param(monkeypatch):
    fake = FakeApi(lambda params: envelope({"totalCount": "0"}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    fetch_mfds.collect_universe(SERVICE_KEY, page_size=100)
    (url, params), = fake.calls
    assert url == fetch_mfds.API_URL
    assert params["serviceKey"] == SERVICE_KEY
    assert params["type"] == "json"
    assert params["numOfRows"] == "100"
    assert "main_item_ingr" not in params
    assert "item_name" not in params


@pytest.mark.parametrize(
    "items",
    [
        {"ITEM_SEQ": "1"},
        [{"ITEM_SEQ": "1"}, {"ITEM_SEQ": "2"}],
        {"item": [{"ITEM_SEQ": "1"}, {"ITEM_SEQ": "2"}]},
    ],
)
def test_body_items_accepts_common_json_envelopes(items):
    assert fetch_mfds.body_items({"items": items})[0]["ITEM_SEQ"] == "1"


def test_universe_pagination_follows_total_count_and_stops(monkeypatch):
    rows = [api_item(i) for i in range(5)]

    def respond(params):
        page, size = int(params["pageNo"]), int(params["numOfRows"])
        chunk = rows[(page - 1) * size: page * size]
        return envelope({"pageNo": page, "numOfRows": size, "totalCount": str(len(rows)), "items": chunk})

    fake = FakeApi(respond)
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    collected = fetch_mfds.collect_universe(SERVICE_KEY, page_size=2)
    assert [item["ITEM_SEQ"] for item in collected] == [str(i) for i in range(5)]
    assert [params["pageNo"] for _, params in fake.calls] == ["1", "2", "3"]


def test_universe_single_dict_items_become_a_one_element_list(monkeypatch):
    fake = FakeApi(lambda params: envelope({"totalCount": "1", "items": api_item(7)}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    collected = fetch_mfds.collect_universe(SERVICE_KEY, page_size=100)
    assert [item["ITEM_SEQ"] for item in collected] == ["7"]
    assert len(fake.calls) == 1


def test_universe_missing_items_means_no_results(monkeypatch):
    fake = FakeApi(lambda params: envelope({"totalCount": "0"}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    assert fetch_mfds.collect_universe(SERVICE_KEY, page_size=100) == []
    assert len(fake.calls) == 1


def test_universe_count_mismatch_raises(monkeypatch):
    """첫 응답의 totalCount와 수집한 고유 ITEM_SEQ 수가 다르면 조용히 넘어가지 않고 실패한다."""
    def respond(params):
        page = int(params["pageNo"])
        items = [api_item(1)] if page == 1 else []
        return envelope({"totalCount": "5", "items": items})

    monkeypatch.setattr(fetch_mfds, "http_get", FakeApi(respond))
    with pytest.raises(RuntimeError, match="불일치"):
        fetch_mfds.collect_universe(SERVICE_KEY, page_size=2)


def test_universe_exact_multiple_of_page_size_stops_normally(monkeypatch):
    """totalCount가 page_size의 정확한 배수면 헛호출 1회(빈 마지막 페이지)가 붙는데,
    그게 페이지 상한 오판으로 실패하면 안 된다."""
    rows = [api_item(i) for i in range(4)]

    def respond(params):
        page, size = int(params["pageNo"]), int(params["numOfRows"])
        chunk = rows[(page - 1) * size: page * size]
        return envelope({"pageNo": page, "numOfRows": size, "totalCount": str(len(rows)), "items": chunk})

    fake = FakeApi(respond)
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    collected = fetch_mfds.collect_universe(SERVICE_KEY, page_size=2)
    assert [item["ITEM_SEQ"] for item in collected] == [str(i) for i in range(4)]
    assert [params["pageNo"] for _, params in fake.calls] == ["1", "2", "3"]


def test_universe_unbounded_full_pages_raises_page_cap_error(monkeypatch):
    """범위 밖 페이지에서 API가 작은 totalCount를 유지한 채 매 페이지마다 새 ITEM_SEQ를 계속 만들어
    꽉 찬 페이지를 준다면(진전 없음 감지에는 걸리지 않지만 종료도 안 되므로) 페이지 상한으로
    무한 루프를 막아야 한다."""
    def respond(params):
        page, size = int(params["pageNo"]), int(params["numOfRows"])
        chunk = [api_item(f"p{page}-{i}") for i in range(size)]
        return envelope({"pageNo": page, "numOfRows": size, "totalCount": "5", "items": chunk})

    fake = FakeApi(respond)
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    with pytest.raises(RuntimeError, match="페이지 상한"):
        fetch_mfds.collect_universe(SERVICE_KEY, page_size=2)
    # 무한 루프가 아니라 상한 근처에서 멈췄는지 확인한다.
    assert len(fake.calls) < 100


def test_universe_repeated_page_raises_no_progress_error(monkeypatch):
    """같은 꽉 찬 페이지가 그대로 반복되면(순환) 새 ITEM_SEQ가 늘지 않아 즉시 실패한다."""
    same_chunk = [api_item(1), api_item(2)]

    def respond(params):
        page, size = int(params["pageNo"]), int(params["numOfRows"])
        return envelope({"pageNo": page, "numOfRows": size, "totalCount": "999999", "items": same_chunk})

    fake = FakeApi(respond)
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    with pytest.raises(RuntimeError, match="진전 없음"):
        fetch_mfds.collect_universe(SERVICE_KEY, page_size=2)
    assert len(fake.calls) == 2


def test_universe_flat_envelope_without_response_wrapper(monkeypatch):
    """실제 DrugPrdtPrmsnInfoService07은 response 래퍼 없이 {header, body}를 반환한다."""
    flat = json.dumps({
        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
        "body": {"totalCount": "1", "items": [api_item(8)]},
    }, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(fetch_mfds, "http_get", FakeApi(lambda params: flat))
    collected = fetch_mfds.collect_universe(SERVICE_KEY, page_size=100)
    assert [item["ITEM_SEQ"] for item in collected] == ["8"]


def test_first_capture_creates_revision(tmp_path):
    when = "2026-08-01T00:00:00Z"
    assert fetch_mfds.merge_item(api_item(11), tmp_path, when) == "new"
    document = json.loads((tmp_path / "11.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["complete"] is True
    assert document["item_seq"] == "11"
    assert document["item_name"] == "제품11"
    assert document["entp_name"] == "제약사"
    assert document["permit_date"] == "20200101"
    assert document["cancel_date"] == ""
    assert document["status"] == "정상"
    assert document["main_item_ingr"] == "성분"
    assert document["edi_code"] == "642101470"
    assert document["atc_code"] == "N02BA01"
    assert document["source_url"] == "https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=11"
    revision, = document["revisions"]
    expected = hashlib.sha256("이 약은 두통에 사용".encode("utf-8")).hexdigest()
    assert revision["content_sha256"] == expected
    assert revision["revision_id"] == f"11-{expected[:8]}"
    assert revision["ee_text"] == "이 약은 두통에 사용"
    assert revision["ee_doc_id"] == "EE-11"
    assert revision["first_observed_at"] == when
    assert revision["last_observed_at"] == when


def test_identical_capture_bumps_last_observed_at_only(tmp_path):
    first, second = "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    fetch_mfds.merge_item(api_item(11), tmp_path, first)
    result = fetch_mfds.merge_item(api_item(11, ITEM_NAME="제품11개명"), tmp_path, second)
    assert result == "unchanged"
    document = json.loads((tmp_path / "11.json").read_text(encoding="utf-8"))
    revision, = document["revisions"]
    assert revision["first_observed_at"] == first
    assert revision["last_observed_at"] == second
    assert document["item_name"] == "제품11개명"


def test_changed_text_appends_revision_newest_first(tmp_path):
    t1, t2, t3 = ("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z")
    fetch_mfds.merge_item(api_item(12, ee="<p>초판</p>"), tmp_path, t1)
    fetch_mfds.merge_item(api_item(12, ee="<p>초판</p>"), tmp_path, t2)
    assert fetch_mfds.merge_item(api_item(12, ee="<p>개정</p>"), tmp_path, t3) == "changed"
    document = json.loads((tmp_path / "12.json").read_text(encoding="utf-8"))
    assert [revision["ee_text"] for revision in document["revisions"]] == ["개정", "초판"]
    assert [revision["first_observed_at"] for revision in document["revisions"]] == [t3, t1]
    assert document["revisions"][0]["last_observed_at"] == t3
    assert document["revisions"][1]["last_observed_at"] == t2
    assert len({revision["revision_id"] for revision in document["revisions"]}) == 2


def test_permit_date_is_normalized_to_yyyymmdd_or_empty(tmp_path):
    fetch_mfds.merge_item(api_item(13, ITEM_PERMIT_DATE="2020-01-01"), tmp_path, "2026-08-01T00:00:00Z")
    document = json.loads((tmp_path / "13.json").read_text(encoding="utf-8"))
    assert document["permit_date"] == ""
    fetch_mfds.merge_item(
        api_item(13, ITEM_PERMIT_DATE="20200304", CANCEL_DATE="20250101", CANCEL_NAME=""), tmp_path,
        "2026-08-02T00:00:00Z",
    )
    document = json.loads((tmp_path / "13.json").read_text(encoding="utf-8"))
    assert document["permit_date"] == "20200304"
    assert document["cancel_date"] == "20250101"
    assert document["status"] == "취소"


def test_skip_without_data_go_key(monkeypatch):
    monkeypatch.delenv("DATA_GO_KEY", raising=False)

    def bomb(url, params=None, retries=3):
        raise AssertionError("API를 호출하면 안 됩니다")

    monkeypatch.setattr(fetch_mfds, "http_get", bomb)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = fetch_mfds.main([])
    assert code == 0
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert "DATA_GO_KEY" in lines[0]


@pytest.mark.parametrize("value", ["202601", "20260230", "99991231"])
def test_changes_since_validation_precedes_missing_key(monkeypatch, value):
    monkeypatch.delenv("DATA_GO_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        fetch_mfds.main(["--changes-since", value])
    assert exc.value.code == 2


def test_valid_changes_since_still_skips_without_key(monkeypatch):
    monkeypatch.delenv("DATA_GO_KEY", raising=False)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert fetch_mfds.main(["--changes-since", "20000101"]) == 0
    assert "DATA_GO_KEY" in buffer.getvalue()


def test_main_persists_items_without_service_key(monkeypatch, tmp_path):
    """--full은 전량 열거(검색어 없는 질의) 뒤 mfds_match로 걸러진 품목만 저장한다."""
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제 (품명: 리보트릴정 등)"],
    ))
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", tmp_path / "items")
    fake = FakeApi(lambda params: envelope({"totalCount": "2", "items": [api_item(21), api_item(22)]}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert fetch_mfds.main(["--full"]) == 0
    assert fake.calls and fake.calls[0][1]["serviceKey"] == SERVICE_KEY
    assert "main_item_ingr" not in fake.calls[0][1]
    assert "item_name" not in fake.calls[0][1]
    files = sorted((tmp_path / "items").glob("*.json"))
    assert [path.name for path in files] == ["21.json", "22.json"]
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert SERVICE_KEY not in content
        document = json.loads(content)
        assert document["schema_version"] == 1
        assert len(document["revisions"]) == 1
    assert "수집 항목=2건" in buffer.getvalue()
    assert "신규 개정=2건" in buffer.getvalue()
    assert "변동 없음=0건" in buffer.getvalue()
    sync = json.loads(fetch_mfds.SYNC_PATH.read_text(encoding="utf-8"))
    assert sync["last_full_run"]
    assert "seen_heads" not in sync


def test_main_respects_max_items(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(tmp_path / "normalized", ["Clonazepam 경구제"]))
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", tmp_path / "items")
    fake = FakeApi(lambda params: envelope({"totalCount": "2", "items": [api_item(31), api_item(32)]}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--full", "--max-items", "1"]) == 0
    assert [path.name for path in sorted((tmp_path / "items").glob("*.json"))] == ["31.json"]


def test_api_error_fails_closed_with_redacted_message(monkeypatch):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)

    def respond(params):
        return json.dumps({"response": {"header": {
            "resultCode": "30",
            "resultMsg": f"SERVICE KEY IS INVALID {SERVICE_KEY}",
        }}}, ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr(fetch_mfds, "http_get", FakeApi(respond))
    with pytest.raises(RuntimeError) as excinfo:
        fetch_mfds.fetch_page(SERVICE_KEY, {"main_item_ingr": "Clonazepam"}, 1, 100)
    assert SERVICE_KEY not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


def test_non_json_response_fails_closed_with_redacted_message(monkeypatch):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(
        fetch_mfds, "http_get",
        lambda url, params=None, retries=3: f"<html>error {SERVICE_KEY}</html>".encode("utf-8"),
    )
    with pytest.raises(RuntimeError) as excinfo:
        fetch_mfds.fetch_page(SERVICE_KEY, {"main_item_ingr": "Clonazepam"}, 1, 100)
    assert SERVICE_KEY not in str(excinfo.value)


def test_verify_mfds_items_accepts_valid_revision(tmp_path):
    items = tmp_path / "items"
    items.mkdir()
    fetch_mfds.merge_item(api_item(41), items, "2026-08-21T00:00:00Z")

    assert verify.validate_mfds_items(items) == []


def test_verify_mfds_items_rejects_changed_text_without_new_hash(tmp_path):
    items = tmp_path / "items"
    items.mkdir()
    fetch_mfds.merge_item(api_item(42), items, "2026-08-21T00:00:00Z")
    path = items / "42.json"
    item = json.loads(path.read_text(encoding="utf-8"))
    item["revisions"][0]["ee_text"] = "변조된 효능·효과"
    path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")

    assert any("SHA-256" in error for error in verify.validate_mfds_items(items))


def test_merge_history_adds_past_revision_and_annotates_current(tmp_path):
    observed = "2026-08-21T00:00:00Z"
    current_item = api_item(51, ee="<DOC><P>현재 적응증</P></DOC>")
    fetch_mfds.merge_item(current_item, tmp_path, observed)
    current_text = fetch_mfds.normalize_ee(current_item["EE_DOC_DATA"])
    current_hash = fetch_mfds.content_sha256(current_text)
    past_text = "과거 적응증"
    past_hash = fetch_mfds.content_sha256(past_text)
    history = [
        {
            "revision_id": f"51-{current_hash[:8]}",
            "content_sha256": current_hash,
            "ee_text": current_text,
            "ee_doc_id": "new",
            "official_revision_date": "2023-06-16",
            "first_observed_at": observed,
            "last_observed_at": observed,
        },
        {
            "revision_id": f"51-{past_hash[:8]}",
            "content_sha256": past_hash,
            "ee_text": past_text,
            "ee_doc_id": "old",
            "official_revision_date": "2020-02-11",
            "first_observed_at": observed,
            "last_observed_at": observed,
        },
    ]

    assert fetch_mfds.merge_history("51", history, tmp_path) == 1
    item = json.loads((tmp_path / "51.json").read_text(encoding="utf-8"))
    assert [revision["ee_text"] for revision in item["revisions"]] == [current_text, past_text]
    assert item["revisions"][0]["official_revision_date"] == "2023-06-16"


def test_merge_history_keeps_undated_current_revision_first(tmp_path):
    """허가이력에 현재 개정이 없으면 현재 개정은 날짜가 없어 뒤로 밀린다."""
    observed = "2026-08-21T00:00:00Z"
    fetch_mfds.merge_item(api_item(52, ee="<DOC><P>현재 적응증</P></DOC>"), tmp_path, observed)
    past_text = "과거 적응증"
    past_hash = fetch_mfds.content_sha256(past_text)
    history = [{
        "revision_id": f"52-{past_hash[:8]}",
        "content_sha256": past_hash,
        "ee_text": past_text,
        "ee_doc_id": "old",
        "official_revision_date": "2099-01-01",
        "first_observed_at": observed,
        "last_observed_at": observed,
    }]

    assert fetch_mfds.merge_history("52", history, tmp_path, observed) == 1
    item = json.loads((tmp_path / "52.json").read_text(encoding="utf-8"))
    assert [revision["ee_text"] for revision in item["revisions"]] == ["현재 적응증", past_text]
    assert "official_revision_date" not in item["revisions"][0]
    assert item["history_fetched_at"] == observed


HISTORY_PAGE = history_page(history_link("7", "2023-06-16", "과거 적응증"))


def test_main_backfills_history_for_unchanged_item_missing_it(monkeypatch, tmp_path):
    """허가이력 없이 저장된 기존 품목은 내용이 그대로여도 한 번은 백필한다."""
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제"],
    ))
    items = tmp_path / "items"
    items.mkdir()
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    fetch_mfds.merge_item(api_item(61), items, "2026-08-01T00:00:00Z")
    history_calls: list[str] = []

    def respond(url, params=None, retries=3):
        if url == fetch_mfds.HISTORY_URL:
            history_calls.append((params or {})["itemSeq"])
            return HISTORY_PAGE
        return envelope({"totalCount": "1", "items": [api_item(61)]})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert fetch_mfds.main(["--full"]) == 0

    assert history_calls == ["61"]
    assert "변동 없음=1건" in buffer.getvalue()
    assert "과거 허가이력=1건" in buffer.getvalue()
    item = json.loads((items / "61.json").read_text(encoding="utf-8"))
    assert [revision["ee_text"] for revision in item["revisions"]] == [
        "이 약은 두통에 사용", "과거 적응증",
    ]

    history_calls.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--full"]) == 0
    assert history_calls == []


def test_history_failure_skips_item_and_disables_after_streak(monkeypatch, tmp_path, capsys):
    """허가이력 실패는 품목 단위로 넘기고, 연속 실패가 쌓이면 이력 수집만 중단한다."""
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제"],
    ))
    items = tmp_path / "items"
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    monkeypatch.setattr(fetch_mfds, "HISTORY_FAILURE_LIMIT", 2)
    rows = [api_item(80 + i) for i in range(4)]
    history_calls: list[str] = []

    def respond(url, params=None, retries=3):
        if url == fetch_mfds.HISTORY_URL:
            history_calls.append((params or {})["itemSeq"])
            raise RuntimeError("Remote end closed connection without response")
        return envelope({"totalCount": str(len(rows)), "items": rows})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    assert fetch_mfds.main(["--full"]) == 0

    out = capsys.readouterr().out
    assert "이력 수집을 중단합니다" in out
    assert "이력 미수집=2건" in out
    assert history_calls == ["80", "81"]  # 연속 2회 실패 후 중단
    # 품목 수집은 계속되고, 이력은 전부 백필 대상으로 남는다
    assert sorted(p.stem for p in items.glob("*.json")) == ["80", "81", "82", "83"]
    for seq in ("80", "81", "82", "83"):
        assert fetch_mfds.history_pending(seq, items) is True


def test_match_failures_are_reported_but_do_not_fail_run(monkeypatch, tmp_path, capsys):
    """매칭 실패 검색어는 건수와 상위 목록으로 보고되고, 매칭된 품목은 그대로 저장된다."""
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized",
        ["Alpha 경구제", "Beta 경구제", "Gamma 경구제"],
    ))
    items = tmp_path / "items"
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    universe = [
        api_item(71, MAIN_INGR_ENG="Alpha"),
        api_item(72, MAIN_INGR_ENG="Gamma"),
        # Beta는 우주에 없어 매칭 실패로 남는다.
    ]

    def respond(url, params=None, retries=3):
        return envelope({"totalCount": str(len(universe)), "items": universe})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    assert fetch_mfds.main(["--full", "--skip-history"]) == 0

    out = capsys.readouterr().out
    assert "매칭 실패=1건" in out
    assert "매칭 실패 검색어 1건" in out
    assert "Beta" in out
    assert sorted(p.stem for p in items.glob("*.json")) == ["71", "72"]


def test_full_enumeration_api_failure_exits_nonzero(monkeypatch, tmp_path):
    """전량 열거 자체가 실패하면 조용한 빈 수집을 막기 위해 크게 실패한다."""
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Alpha 경구제"],
    ))
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", tmp_path / "items")

    def respond(url, params=None, retries=3):
        raise RuntimeError("MFDS API 오류: 코드=01, 메시지=System Error!!")

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--full", "--skip-history"]) == 1


def test_incremental_mode_uses_change_feed(monkeypatch, tmp_path, capsys):
    """동기화 상태가 있으면 검색 대신 변경분 질의로 갱신·유관 신규만 처리한다."""
    real_executor = fetch_mfds.ThreadPoolExecutor
    worker_counts = []

    def recording_executor(*args, **kwargs):
        worker_counts.append(kwargs.get("max_workers"))
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(fetch_mfds, "ThreadPoolExecutor", recording_executor)
    monkeypatch.setattr(fetch_mfds, "today_kst", lambda: "20200131")
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제"],
    ))
    items = tmp_path / "items"
    items.mkdir()
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    # 기존 저장 품목(클로나제팔)과 동기화 상태를 준비한다
    fetch_mfds.merge_item(
        api_item(61, MAIN_INGR_ENG="Clonazepam"), items, "2026-08-01T00:00:00Z",
    )
    fetch_mfds.save_sync("20260820", None)

    changed_rows = [
        api_item(61, ee="<p>개정된 적응증</p>", MAIN_INGR_ENG="Clonazepam"),  # 기존 품목의 갱신
        api_item(62, ITEM_NAME="새클로정", MAIN_INGR_ENG="Clonazepam Hydrochloride"),   # 매칭(접두 일치)
        api_item(63, ITEM_NAME="무관정", MAIN_INGR_ENG="Metformin Hydrochloride"),     # 무관 성분 → 제외
    ]

    def respond(url, params=None, retries=3):
        params = params or {}
        if url == fetch_mfds.HISTORY_URL:
            raise RuntimeError("이번 테스트는 이력 없음")
        if "start_change_date" in params:
            assert params["start_change_date"] == "20200101"
            return envelope({"totalCount": str(len(changed_rows)), "items": changed_rows})
        return envelope({"totalCount": "0"})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    assert fetch_mfds.main([
        "--skip-history", "--incremental-workers", "2", "--changes-since", "20200101",
    ]) == 0

    assert worker_counts == [2]
    assert sorted(p.stem for p in items.glob("*.json")) == ["61", "62"]
    updated = json.loads((items / "61.json").read_text(encoding="utf-8"))
    assert updated["revisions"][0]["ee_text"] == "개정된 적응증"
    sync = json.loads(fetch_mfds.SYNC_PATH.read_text(encoding="utf-8"))
    assert sync["last_change_date"] == "20260820"
    assert "변동 없음=0건" in capsys.readouterr().out


def test_incremental_change_feed_failure_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제"],
    ))
    items = tmp_path / "items"
    items.mkdir()
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    fetch_mfds.merge_item(api_item(61), items, "2026-08-01T00:00:00Z")
    fetch_mfds.save_sync("20260820", None)

    def respond(url, params=None, retries=3):
        if "start_change_date" in (params or {}):
            raise RuntimeError("MFDS API 오류: 코드=01, 메시지=System Error!!")
        return envelope({"totalCount": "0"})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--skip-history"]) == 1


def test_main_skip_history_leaves_item_pending_backfill(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제"],
    ))
    items = tmp_path / "items"
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)

    def bomb(url, params=None, retries=3):
        assert url != fetch_mfds.HISTORY_URL, "--skip-history에서는 허가이력을 받지 않습니다"
        return envelope({"totalCount": "1", "items": [api_item(71)]})

    monkeypatch.setattr(fetch_mfds, "http_get", bomb)
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--full", "--skip-history"]) == 0

    assert fetch_mfds.history_pending("71", items) is True


def test_backfill_resumes_after_last_completed_month(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "today_kst", lambda: "20200229")
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제"],
    ))
    items = tmp_path / "items"
    items.mkdir()
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    fetch_mfds.merge_item(
        api_item(81, MAIN_INGR_ENG="Clonazepam"), items, "2020-01-01T00:00:00Z",
    )
    fetch_mfds.save_sync("20200229", None)
    calls = []

    def fail_february(url, params=None, retries=3):
        params = params or {}
        if "start_change_date" not in params:
            return envelope({"totalCount": "0"})
        calls.append(params["start_change_date"])
        if params["start_change_date"] == "20200201":
            raise RuntimeError("temporary")
        return envelope({"totalCount": "0", "items": []})

    monkeypatch.setattr(fetch_mfds, "http_get", fail_february)
    assert fetch_mfds.main(["--changes-since", "20200101", "--skip-history"]) == 1
    state = json.loads(fetch_mfds.BACKFILL_PATH.read_text(encoding="utf-8"))
    assert calls == ["20200101", "20200201"]
    assert state["next_date"] == "20200201"

    calls.clear()
    monkeypatch.setattr(
        fetch_mfds, "http_get",
        lambda url, params=None, retries=3: (
            calls.append(params["start_change_date"])
            or envelope({"totalCount": "0", "items": []})
        ),
    )
    assert fetch_mfds.main(["--changes-since", "20200101", "--skip-history"]) == 0
    assert calls == ["20200201"]
    assert not fetch_mfds.BACKFILL_PATH.exists()


def test_unchanged_recapture_keeps_official_revision_date(tmp_path):
    """허가이력 병합 뒤 같은 내용을 다시 받아도 official_revision_date·ee_doc_id가 남아야 한다."""
    observed = "2026-08-21T00:00:00Z"
    item = api_item(91, ee="<DOC><P>현재 적응증</P></DOC>")
    fetch_mfds.merge_item(item, tmp_path, observed)
    text = fetch_mfds.normalize_ee(item["EE_DOC_DATA"])
    digest = fetch_mfds.content_sha256(text)
    fetch_mfds.merge_history("91", [{
        "revision_id": f"91-{digest[:8]}", "content_sha256": digest, "ee_text": text,
        "ee_doc_id": "official-9", "official_revision_date": "2023-06-16",
        "first_observed_at": observed, "last_observed_at": observed,
    }], tmp_path, observed)

    assert fetch_mfds.merge_item(item, tmp_path, "2026-08-22T00:00:00Z") == "unchanged"
    (revision,) = json.loads((tmp_path / "91.json").read_text(encoding="utf-8"))["revisions"]
    assert revision["official_revision_date"] == "2023-06-16"
    assert revision["ee_doc_id"] == "official-9"
    assert revision["first_observed_at"] == observed
    assert revision["last_observed_at"] == "2026-08-22T00:00:00Z"
    assert json.loads((tmp_path / "91.json").read_text(encoding="utf-8"))["history_fetched_at"] == observed


def test_reverted_text_moves_known_revision_to_front_without_duplicate(tmp_path):
    """A→B→A 복귀는 같은 해시를 두 번 넣지 않고 기존 개정을 현행으로 되돌린다(verify 중복 오류 방지)."""
    t1, t2, t3 = "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z"
    fetch_mfds.merge_item(api_item(92, ee="<p>A</p>"), tmp_path, t1)
    fetch_mfds.merge_item(api_item(92, ee="<p>B</p>"), tmp_path, t2)
    assert fetch_mfds.merge_item(api_item(92, ee="<p>A</p>"), tmp_path, t3) == "changed"
    document = json.loads((tmp_path / "92.json").read_text(encoding="utf-8"))
    assert [revision["ee_text"] for revision in document["revisions"]] == ["A", "B"]
    assert document["revisions"][0]["first_observed_at"] == t1
    assert document["revisions"][0]["last_observed_at"] == t3
    assert verify.validate_mfds_items(tmp_path) == []


def test_change_feed_uses_mfds_match_for_relevance(monkeypatch, tmp_path):
    """변경분 행만으로 mfds_match가 고시 검색어와 연결된 품목만 골라낸다."""
    monkeypatch.setattr(fetch_mfds, "today_kst", lambda: "20260901")
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(tmp_path / "normalized", ["Alpha 경구제"]))
    items = tmp_path / "items"
    items.mkdir()
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    fetch_mfds.merge_item(api_item(1, MAIN_INGR_ENG="Alpha"), items, "2026-08-01T00:00:00Z")
    fetch_mfds.save_sync("20260820", None)
    changed_rows = [
        api_item(3, MAIN_INGR_ENG="Alpha Hydrochloride"),   # 검색어 Alpha의 접두 일치 → 유관
        api_item(4, MAIN_INGR_ENG="Gamma"),                 # 연결 없는 성분 → 제외
    ]

    def respond(url, params=None, retries=3):
        params = params or {}
        if "start_change_date" in params:
            return envelope({"totalCount": str(len(changed_rows)), "items": changed_rows})
        return envelope({"totalCount": "0"})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--skip-history"]) == 0
    assert sorted(p.stem for p in items.glob("*.json")) == ["1", "3"]


def test_pending_history_is_backfilled_after_change_feed(monkeypatch, tmp_path, capsys):
    """변경분에 다시 나타나지 않는 이력 미수집 품목도 실행 끝에 상한 안에서 백필한다."""
    monkeypatch.setattr(fetch_mfds, "today_kst", lambda: "20260901")
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(tmp_path / "normalized", ["Alpha 경구제"]))
    items = tmp_path / "items"
    items.mkdir()
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", items)
    for seq in (1, 2, 3):
        fetch_mfds.merge_item(api_item(seq, MAIN_INGR_ENG="Alpha"), items, "2026-08-01T00:00:00Z")
    fetch_mfds.save_sync("20260820", None)
    history_calls: list[str] = []

    def respond(url, params=None, retries=3):
        params = params or {}
        if url == fetch_mfds.HISTORY_URL:
            history_calls.append(params["itemSeq"])
            return HISTORY_PAGE
        return envelope({"totalCount": "0"})

    monkeypatch.setattr(fetch_mfds, "http_get", respond)
    assert fetch_mfds.main(["--history-backfill-limit", "2"]) == 0
    assert history_calls == ["1", "2"]
    assert fetch_mfds.history_pending("3", items) is True
    assert "과거 허가이력=2건" in capsys.readouterr().out

    history_calls.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--history-backfill-limit", "2"]) == 0
    assert history_calls == ["3"]


def test_max_items_truncation_does_not_advance_sync(monkeypatch, tmp_path):
    """상한으로 잘린 실행이 전량 열거를 완료로 기록하면 미처리 품목이 영원히 빠진다."""
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(tmp_path / "normalized", ["Clonazepam 경구제"]))
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", tmp_path / "items")
    fake = FakeApi(lambda params: envelope({"totalCount": "2", "items": [api_item(31), api_item(32)]}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert fetch_mfds.main(["--full", "--max-items", "1", "--skip-history"]) == 0
    assert not fetch_mfds.SYNC_PATH.exists()
    assert "동기화 상태를 갱신하지 않습니다" in buffer.getvalue()


def test_whitespace_only_differences_do_not_create_phantom_revisions(tmp_path):
    """API 본문과 변경이력 본문이 띄어쓰기만 다르면 같은 개정으로 본다(저장 자료의 1%가 이 유령 개정)."""
    observed = "2026-08-21T00:00:00Z"
    fetch_mfds.merge_item(api_item(93, ee="<DOC><P>1. 수분 보급</P><P>2. 희석제</P></DOC>"), tmp_path, observed)
    spaced = "1. 수분 보급  2. 희석제"
    fetch_mfds.merge_history("93", [{
        "revision_id": f"93-{fetch_mfds.content_sha256(spaced)[:8]}", "content_sha256": fetch_mfds.content_sha256(spaced),
        "ee_text": spaced, "ee_doc_id": "19", "official_revision_date": "2017-09-20",
        "first_observed_at": observed, "last_observed_at": observed,
    }], tmp_path, observed)
    (revision,) = json.loads((tmp_path / "93.json").read_text(encoding="utf-8"))["revisions"]
    assert revision["official_revision_date"] == "2017-09-20"
    assert fetch_mfds.merge_item(api_item(93, ee="<DOC><P>1.  수분 보급</P><P>2. 희석제</P></DOC>"), tmp_path, "2026-08-22T00:00:00Z") == "unchanged"
    assert len(json.loads((tmp_path / "93.json").read_text(encoding="utf-8"))["revisions"]) == 1
