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
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetch_mfds, "REQUEST_SLEEP", 0)


def test_normalize_ee_strips_tags_unescapes_and_collapses():
    raw = "<p>이 약은 &amp; 그람양성</p>\n<br/>균에 \t 작용합니다 .&nbsp;"
    assert fetch_mfds.normalize_ee(raw) == "이 약은 & 그람양성 균에 작용합니다 ."
    assert fetch_mfds.normalize_ee(None) == ""


def test_content_hash_is_sha256_of_normalized_text():
    text = fetch_mfds.normalize_ee("<p>두통</p>")
    assert fetch_mfds.content_sha256(text) == hashlib.sha256("두통".encode("utf-8")).hexdigest()


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


def test_request_carries_service_key_and_single_search_param(monkeypatch):
    fake = FakeApi(lambda params: envelope({"totalCount": "0"}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    fetch_mfds.collect_pages(SERVICE_KEY, "main_item_ingr", "Clonazepam", 100, None)
    (url, params), = fake.calls
    assert url == fetch_mfds.API_URL
    assert params["serviceKey"] == SERVICE_KEY
    assert params["type"] == "json"
    assert params["numOfRows"] == "100"
    assert params["main_item_ingr"] == "Clonazepam"
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


def test_pagination_follows_total_count_and_stops(monkeypatch):
    rows = [api_item(i) for i in range(5)]

    def respond(params):
        page, size = int(params["pageNo"]), int(params["numOfRows"])
        chunk = rows[(page - 1) * size: page * size]
        return envelope({"pageNo": page, "numOfRows": size, "totalCount": str(len(rows)), "items": chunk})

    fake = FakeApi(respond)
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    collected = fetch_mfds.collect_pages(SERVICE_KEY, "main_item_ingr", "Clonazepam", 2, None)
    assert [item["ITEM_SEQ"] for item in collected] == [str(i) for i in range(5)]
    assert [params["pageNo"] for _, params in fake.calls] == ["1", "2", "3"]


def test_single_dict_items_become_a_one_element_list(monkeypatch):
    fake = FakeApi(lambda params: envelope({"totalCount": "1", "items": api_item(7)}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    collected = fetch_mfds.collect_pages(SERVICE_KEY, "main_item_ingr", "Clonazepam", 100, None)
    assert [item["ITEM_SEQ"] for item in collected] == ["7"]
    assert len(fake.calls) == 1


def test_missing_items_means_no_results(monkeypatch):
    fake = FakeApi(lambda params: envelope({"totalCount": "0"}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    assert fetch_mfds.collect_pages(SERVICE_KEY, "main_item_ingr", "Clonazepam", 100, None) == []
    assert len(fake.calls) == 1


def test_max_items_truncates_collection(monkeypatch):
    rows = [api_item(i) for i in range(10)]
    fake = FakeApi(lambda params: envelope({"totalCount": "10", "items": rows}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    collected = fetch_mfds.collect_pages(SERVICE_KEY, "main_item_ingr", "Clonazepam", 100, 3)
    assert [item["ITEM_SEQ"] for item in collected] == ["0", "1", "2"]
    assert len(fake.calls) == 1


def test_item_name_fallback_after_empty_primary(monkeypatch):
    def respond(params):
        if params.get("main_item_ingr") or params.get("item_name") == "Clonazepam":
            return envelope({"totalCount": "0"})
        return envelope({"totalCount": "1", "items": api_item(9, ITEM_NAME="리보트릴정")})

    fake = FakeApi(respond)
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    rows, search_param = fetch_mfds.collect_term(
        SERVICE_KEY, "Clonazepam", ["리보트릴정"], 100, None,
    )
    assert search_param == "item_name"
    assert [item["ITEM_NAME"] for item in rows] == ["리보트릴정"]
    attempted = [
        (key, value)
        for _, params in fake.calls
        for key, value in params.items()
        if key in {"main_item_ingr", "item_name"}
    ]
    assert attempted == [
        ("main_item_ingr", "Clonazepam"),
        ("item_name", "Clonazepam"),
        ("item_name", "리보트릴정"),
    ]


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


def test_main_persists_items_without_service_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(
        tmp_path / "normalized", ["Clonazepam 경구제 (품명: 리보트릴정 등)"],
    ))
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", tmp_path / "items")
    fake = FakeApi(lambda params: envelope({"totalCount": "2", "items": [api_item(21), api_item(22)]}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert fetch_mfds.main(["--max-terms", "1"]) == 0
    assert fake.calls and fake.calls[0][1]["serviceKey"] == SERVICE_KEY
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


def test_main_respects_max_items(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(fetch_mfds, "NORMALIZED_DIR", write_normalized(tmp_path / "normalized", ["Clonazepam 경구제"]))
    monkeypatch.setattr(fetch_mfds, "ITEMS_DIR", tmp_path / "items")
    fake = FakeApi(lambda params: envelope({"totalCount": "2", "items": [api_item(31), api_item(32)]}))
    monkeypatch.setattr(fetch_mfds, "http_get", fake)
    with contextlib.redirect_stdout(io.StringIO()):
        assert fetch_mfds.main(["--max-terms", "1", "--max-items", "1"]) == 0
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
        fetch_mfds.fetch_page(SERVICE_KEY, "main_item_ingr", "Clonazepam", 1, 100)
    assert SERVICE_KEY not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


def test_non_json_response_fails_closed_with_redacted_message(monkeypatch):
    monkeypatch.setenv("DATA_GO_KEY", SERVICE_KEY)
    monkeypatch.setattr(
        fetch_mfds, "http_get",
        lambda url, params=None, retries=3: f"<html>error {SERVICE_KEY}</html>".encode("utf-8"),
    )
    with pytest.raises(RuntimeError) as excinfo:
        fetch_mfds.fetch_page(SERVICE_KEY, "main_item_ingr", "Clonazepam", 1, 100)
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
