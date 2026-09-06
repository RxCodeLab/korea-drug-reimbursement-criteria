from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from common import DATA, DB_PATH, RAW, has_credential_query

NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
# 제목이 이보다 길면 본문이 제목으로 샐킨 것이다. 실제 제목은 복합제·품명 나열을 포함해도 200자를 넘지 않는다.
MAX_TITLE_CHARS = 200
RE_ITEM_HEADER_LINE = re.compile(r"^\[(\d{3}|일반원칙)\]$")
REQUIRED_ROLES = {"annex", "notice"}


def digest(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            h.update(block)
    return size, h.hexdigest()


def entry_boundary_errors(document: dict) -> list[str]:
    """정규화 문서 한 건의 항목 경계 오류. 제목에 본문이 들어가거나 본문에 다른 항목이 합쳐진 경우를 걸러낸다."""
    errors: list[str] = []
    entries = document["entries"]
    for entry in entries:
        title = entry["title"]
        if not entry["class_no"]:
            continue
        if len(title) > MAX_TITLE_CHARS:
            errors.append(f"항목 제목이 {MAX_TITLE_CHARS}자를 넘습니다(본문이 제목에 섮임): {title[:60]}…")
        for line in entry["body"].split("\n"):
            if RE_ITEM_HEADER_LINE.fullmatch(line.strip()):
                errors.append(f"항목 본문에 다른 항목 헤더 {line.strip()}이 들어 있습니다: {title[:60]}")
                break
    entry_counts: dict[int, int] = {}
    for entry in entries:
        entry_counts[entry["attachment_ordinal"]] = entry_counts.get(entry["attachment_ordinal"], 0) + 1
    for attachment in document["attachments"]:
        if (attachment["role"] in REQUIRED_ROLES and attachment["parser_status"] == "complete"
                and not entry_counts.get(attachment["ordinal"])):
            errors.append(f"필수 첨부 {attachment['stored_name']}을(를) 파싱했다고 하지만 항목이 하나도 없습니다")
    return errors


def validate_mfds_items(items_dir: Path = MFDS_ITEMS) -> list[str]:
    errors: list[str] = []
    for path in sorted(items_dir.glob("*.json")) if items_dir.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            sequence = str(item["item_seq"])
            if item.get("schema_version") != 1 or item.get("complete") is not True:
                raise ValueError("schema-v1 MFDS 품목 형식에 맞지 않습니다")
            if path.stem != sequence:
                raise ValueError("파일명과 품목기준코드가 다릅니다")
            if has_credential_query(item["source_url"]):
                raise ValueError("출처 URL에 인증정보가 들어 있습니다")
            revisions = item["revisions"]
            if not isinstance(revisions, list) or not revisions:
                raise ValueError("효능·효과 관찰 이력이 없습니다")
            seen: set[str] = set()
            for revision in revisions:
                text = revision["ee_text"]
                content_hash = revision["content_sha256"]
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != content_hash:
                    raise ValueError("효능·효과 SHA-256이 일치하지 않습니다")
                if revision["revision_id"] != f"{sequence}-{content_hash[:8]}":
                    raise ValueError("허가사항 개정 식별자가 일치하지 않습니다")
                if content_hash in seen:
                    raise ValueError("같은 효능·효과 개정이 중복되었습니다")
                seen.add(content_hash)
                if not revision["first_observed_at"] or not revision["last_observed_at"]:
                    raise ValueError("관찰 시각이 없습니다")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    return errors


def validate() -> list[str]:
    errors: list[str] = []
    metas: dict[str, dict] = {}
    for meta_path in sorted(RAW.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("schema_version") != 1 or meta.get("complete") is not True:
                raise ValueError("schema-v1 매니페스트 형식에 맞지 않습니다")
            version = meta["version"]
            sequence = version["행정규칙일련번호"]
            if sequence in metas:
                raise ValueError(f"일련번호가 중복되었습니다: {sequence}")
            attachments = meta["attachments"]
            if not attachments:
                raise ValueError("첨부파일이 없습니다")
            ordinals: set[int] = set()
            for attachment in attachments:
                ordinal = attachment["ordinal"]
                if ordinal in ordinals:
                    raise ValueError(f"첨부파일 순번이 중복되었습니다: {ordinal}")
                ordinals.add(ordinal)
                if attachment.get("status") != "complete" or has_credential_query(attachment["source_url"]):
                    raise ValueError(f"안전하지 않거나 완료되지 않은 첨부파일입니다: {ordinal}")
                file_path = meta_path.parent / attachment["stored_name"]
                size, sha256 = digest(file_path)
                if size != attachment["size"] or sha256 != attachment["sha256"]:
                    raise ValueError(f"첨부파일 SHA-256이 일치하지 않습니다: {ordinal}")
            metas[sequence] = meta
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{meta_path}: {exc}")

    normalized: dict[str, dict] = {}
    for path in sorted(NORMALIZED.glob("*.json")) if NORMALIZED.exists() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            sequence = document["version"]["행정규칙일련번호"]
            if document.get("complete") is not True or sequence not in metas:
                raise ValueError("원본이 없거나 내용이 온전하지 않은 정규화 문서입니다")
            if document["version"] != metas[sequence]["version"]:
                raise ValueError("정규화 문서의 고시 정보가 원본 매니페스트와 다릅니다")
            source = {a["ordinal"]: a["sha256"] for a in metas[sequence]["attachments"]}
            parsed = {a["ordinal"]: a for a in document["attachments"]}
            if set(source) != set(parsed):
                raise ValueError("첨부파일 출처 집합이 일치하지 않습니다")
            if any(parsed[o]["sha256"] != sha for o, sha in source.items()):
                raise ValueError("첨부파일 출처 SHA-256이 일치하지 않습니다")
            for entry in document["entries"]:
                if source.get(entry["attachment_ordinal"]) != entry["attachment_sha256"]:
                    raise ValueError("항목의 출처 정보가 일치하지 않습니다")
            boundary_errors = entry_boundary_errors(document)
            if boundary_errors:
                raise ValueError("; ".join(boundary_errors))
            normalized[sequence] = document
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    if set(metas) != set(normalized):
        missing = sorted(set(metas) - set(normalized))
        extra = sorted(set(normalized) - set(metas))
        errors.append(f"정규화 문서 범위가 일치하지 않습니다: 누락={missing} 추가={extra}")

    errors.extend(validate_mfds_items())

    errors.extend(validate_database(normalized))
    return errors


def validate_database(normalized: dict[str, dict]) -> list[str]:
    if not DB_PATH.is_file():
        return [f"데이터베이스가 없습니다: {DB_PATH}"]
    expected_entries = sum(len(document["entries"]) for document in normalized.values())
    expected_keys = {
        (sequence, entry["attachment_ordinal"], entry["block_identity"])
        for sequence, document in normalized.items()
        for entry in document["entries"]
    }
    errors: list[str] = []
    if len(expected_keys) != expected_entries:
        errors.append("정규화 항목 식별자(일련번호, 첨부 순번, block_identity)가 중복됩니다")
    # FTS5 integrity-check는 INSERT 구문이라 쓰기 가능한 연결이 필요하다. 게시 직전 관문이 검사 대상을 쓸 수 있어서는
    # 안 되므로 임시 사본을 검사하고 원본은 손대지 않는다.
    try:
        with tempfile.TemporaryDirectory(prefix="verify-db-") as scratch:
            copy = Path(scratch) / DB_PATH.name
            shutil.copyfile(DB_PATH, copy)
            errors.extend(_check_database_copy(copy, normalized, expected_entries, expected_keys))
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"SQLite 검증 실패: {exc}")
    return errors


def _check_database_copy(
    path: Path, normalized: dict[str, dict], expected_entries: int, expected_keys: set[tuple[str, int, str]],
) -> list[str]:
    errors: list[str] = []
    con = sqlite3.connect(path)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"SQLite integrity_check 결과: {integrity}")
        foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            errors.append(f"SQLite foreign_key_check 결과: {foreign_keys[:5]}")
        versions = con.execute("SELECT count(*) FROM versions").fetchone()[0]
        if versions != len(normalized):
            errors.append(f"SQLite 버전 수 {versions}개가 정규화 문서 수 {len(normalized)}개와 다릅니다")
        stored_keys = set(con.execute(
            "SELECT v.일련번호, a.ordinal, e.block_identity FROM entries e "
            "JOIN versions v ON v.ver_id = e.ver_id JOIN attachments a ON a.attachment_id = e.attachment_id"
        ).fetchall())
        if len(stored_keys) != expected_entries or stored_keys != expected_keys:
            missing = sorted(expected_keys - stored_keys)[:3]
            extra = sorted(stored_keys - expected_keys)[:3]
            errors.append(
                f"SQLite 항목 {len(stored_keys)}개가 정규화 항목 {expected_entries}개와 일치하지 않습니다"
                f" (누락 예={missing} 추가 예={extra})"
            )
        # 외부 content FTS는 count(*)가 원본 테이블을 읽으므로 색인 자체를 검사해야 한다. 실패하면 예외가 난다.
        con.execute("INSERT INTO fts(fts) VALUES('integrity-check')")
        for sequence, document in normalized.items():
            for entry in document["entries"][:1]:
                matched = con.execute(
                    "SELECT count(*) FROM fts f JOIN entries e ON e.id = f.rowid JOIN versions v ON v.ver_id = e.ver_id "
                    "WHERE v.일련번호 = ? AND e.block_identity = ? AND fts MATCH ?",
                    (sequence, entry["block_identity"], _fts_phrase(entry["title"])),
                ).fetchone()[0]
                if matched != 1:
                    errors.append(f"FTS 색인이 항목 제목을 찾지 못합니다: {sequence} {entry['title'][:40]}")
                    break
    finally:
        con.close()
    return errors


def _fts_phrase(text: str) -> str:
    """제목의 첫 단어를 FTS5 구문 인용 토큰으로 만든다."""
    words = [w for w in re.split(r"[^0-9A-Za-z가-힣]+", text) if w]
    return '"' + (words[0] if words else text).replace('"', '""') + '"'


def main() -> None:
    errors = validate()
    if errors:
        for error in errors:
            print(f"[오류] {error}", file=sys.stderr)
        raise SystemExit(f"검증에서 오류 {len(errors)}건이 발생했습니다")
    print("검증을 통과했습니다")


if __name__ == "__main__":
    main()
