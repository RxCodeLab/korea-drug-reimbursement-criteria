from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from common import DATA, DB_PATH, RAW
from documents import PARSER_VERSION, ExtractionError, extract_document

NORMALIZED = DATA / "normalized"
_SCHEMA_VERSION = 1
_REQUIRED_ROLES = {"annex", "notice"}
_ROLES = {"annex", "notice", "qa", "transition", "reason", "comparison", "other"}
_FORMAT_RANK = {"hwpx": 0, "hwp": 1, "pdf": 2}

RE_CLASS_HEADER = re.compile(r"^\[(\d{3}|일반원칙)\]\s*(\S.*)$")
RE_ITEM_NO = re.compile(r"^\[(\d{3}|일반원칙)\]$")
RE_ACTION = re.compile(r"\[\s*(신\s*설|변\s*경|삭\s*제)\s*\]")
RE_PUMMYEONG = re.compile(r"\(품명\s*[::]")
RE_NUMBERED_CONDITION = re.compile(r"^\d+[.)]")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(meta: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({"version": meta["version"], "attachments": meta["attachments"]})).hexdigest()


def _safe_url(url: str) -> bool:
    if "law_oc" in url.casefold():
        return False
    try:
        return all(key.casefold() != "oc" for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except ValueError:
        return False


def _validate_meta(path: Path) -> dict[str, Any]:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"메타데이터가 잘못되었습니다: {path}") from exc
    if meta.get("schema_version") != _SCHEMA_VERSION or meta.get("complete") is not True:
        raise RuntimeError(f"메타데이터가 불완전하거나 지원하지 않는 형식입니다: {path}")
    version = meta.get("version")
    attachments = meta.get("attachments")
    required_version = {"행정규칙일련번호", "시행일자", "발령일자", "발령번호", "행정규칙명"}
    if not isinstance(version, dict) or not required_version <= version.keys() or not isinstance(attachments, list):
        raise RuntimeError(f"메타데이터가 표준 구조를 충족하지 않습니다: {path}")
    seen_ordinals: set[int] = set()
    for attachment in attachments:
        required = {"ordinal", "source_url", "original_name", "stored_name", "format", "role", "size", "sha256", "status"}
        if not isinstance(attachment, dict) or not required <= attachment.keys():
            raise RuntimeError(f"첨부파일 정보가 표준 구조를 충족하지 않습니다: {path}")
        if attachment["status"] != "complete" or attachment["format"] not in _FORMAT_RANK or attachment["role"] not in _ROLES:
            raise RuntimeError(f"지원하지 않거나 완료되지 않은 첨부파일입니다: {path}")
        if not isinstance(attachment["ordinal"], int) or attachment["ordinal"] in seen_ordinals:
            raise RuntimeError(f"첨부파일 순번이 중복되었거나 잘못되었습니다: {path}")
        if not _safe_url(str(attachment["source_url"])):
            raise RuntimeError(f"인증 정보가 포함된 원본 URL은 허용되지 않습니다: {path}")
        seen_ordinals.add(attachment["ordinal"])
    return meta


def _normalized_stem(name: str) -> str:
    stem = unicodedata.normalize("NFKC", Path(name).stem).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", stem)


def select_renditions(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for attachment in attachments:
        groups.setdefault((_normalized_stem(attachment["original_name"]), attachment["role"]), []).append(attachment)
    return [
        min(group, key=lambda item: (_FORMAT_RANK[item["format"]], item["ordinal"]))
        for _, group in sorted(groups.items())
    ]


def split_blocks(text: str) -> list[dict[str, str]]:
    lines = text.split("\n")
    blocks: list[dict[str, Any]] = []
    action = ""
    class_header = ""
    current: dict[str, Any] | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = RE_ACTION.search(line)
        if match and len(line) < 80:
            action = re.sub(r"\s", "", match.group(1))
            i += 1
            continue
        match = RE_CLASS_HEADER.match(line)
        if match and not RE_PUMMYEONG.search(line):
            class_header = line
        item = RE_ITEM_NO.match(line)
        if item:
            if current:
                blocks.append(current)
            title_lines: list[str] = []
            j = i + 1
            while j < len(lines) and len(title_lines) < 8:
                candidate = lines[j].strip()
                if RE_ITEM_NO.match(candidate) or RE_NUMBERED_CONDITION.match(candidate):
                    break
                title_lines.append(candidate)
                title = " ".join(title_lines)
                if RE_PUMMYEONG.search(title) and title.endswith(")"):
                    j += 1
                    break
                if item.group(1) == "일반원칙" and candidate:
                    j += 1
                    break
                j += 1
            current = {"action": action, "class_no": item.group(1), "class_header": class_header,
                       "title": " ".join(title_lines).strip(), "body_lines": []}
            i = j
            continue
        if current is not None:
            current["body_lines"].append(lines[i])
        i += 1
    if current:
        blocks.append(current)

    result: list[dict[str, str]] = []
    for block in blocks:
        title = re.sub(r"\s+", " ", block["title"]).strip()
        body = "\n".join(block["body_lines"]).strip()
        compact_title = re.sub(r"\s+", "", title)
        if not title or compact_title in {"구분", "품명", "성분명"} or (not body and not RE_PUMMYEONG.search(title)):
            continue
        result.append({"action": block["action"], "class_no": block["class_no"],
                       "class_header": block["class_header"], "title": title, "body": body})
    return result


def norm_title(class_no: str, title: str) -> str:
    head = re.split(r"\(품명", title)[0]
    return f"[{class_no}]" + re.sub(r"[\s··]+", "", head).lower()


def _attachment_path(version_dir: Path, attachment: dict[str, Any]) -> Path:
    path = version_dir / attachment["stored_name"]
    if path.parent != version_dir or not path.is_file():
        raise RuntimeError(f"첨부파일 경로가 잘못되었거나 파일이 없습니다: {path}")
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != attachment["sha256"] or path.stat().st_size != attachment["size"]:
        raise RuntimeError(f"첨부파일 SHA-256 또는 크기가 일치하지 않습니다: {path}")
    return path


def _parse_version(version_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    fingerprint = _fingerprint(meta)
    output_path = NORMALIZED / f"{meta['version']['행정규칙일련번호']}.json"
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            if (existing.get("complete") is True and existing.get("schema_version") == _SCHEMA_VERSION
                    and existing.get("input_fingerprint") == fingerprint
                    and existing.get("parser_version") == PARSER_VERSION):
                return existing
        except (OSError, json.JSONDecodeError):
            pass

    selected = {item["ordinal"] for item in select_renditions(meta["attachments"])}
    output_attachments: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    seen_blocks: set[tuple[int, str]] = set()
    for attachment in sorted(meta["attachments"], key=lambda item: item["ordinal"]):
        record = dict(attachment)
        record.update({"parser_version": PARSER_VERSION, "selected": attachment["ordinal"] in selected,
                       "parser_status": "not_selected"})
        if attachment["ordinal"] not in selected:
            output_attachments.append(record)
            continue
        try:
            text = extract_document(_attachment_path(version_dir, attachment), attachment["format"])
            if not text.strip():
                raise ExtractionError("문서에서 텍스트를 추출하지 못했습니다")
            record["parser_status"] = "complete"
            if attachment["role"] == "annex":
                parsed = split_blocks(text)
                if not parsed:
                    raise ExtractionError("별지에서 유효한 기준 블록을 찾지 못했습니다")
                for block in parsed:
                    identity = (attachment["ordinal"], norm_title(block["class_no"], block["title"]))
                    if identity in seen_blocks:
                        raise RuntimeError(f"{version_dir.name}에 중복된 블록 식별자가 있습니다: {identity[1]}")
                    seen_blocks.add(identity)
                    entries.append({**block, "attachment_ordinal": attachment["ordinal"],
                                    "attachment_sha256": attachment["sha256"], "block_identity": identity[1]})
            else:
                identity = f"__{attachment['role']}__{attachment['sha256']}"
                entries.append({"action": attachment["role"], "class_no": "", "class_header": "",
                                "title": attachment["original_name"], "body": text,
                                "attachment_ordinal": attachment["ordinal"], "attachment_sha256": attachment["sha256"],
                                "block_identity": identity})
        except (ExtractionError, RuntimeError) as exc:
            record["parser_status"] = "failed"
            record["parser_error"] = str(exc)
            if attachment["role"] in _REQUIRED_ROLES:
                raise RuntimeError(f"필수 {attachment['role']} 파일을 파싱하지 못했습니다: {version_dir.name}/{attachment['stored_name']}: {exc}") from exc
        output_attachments.append(record)
    normalized = {"schema_version": _SCHEMA_VERSION, "complete": True, "input_fingerprint": fingerprint,
                  "parser_version": PARSER_VERSION, "version": meta["version"],
                  "attachments": output_attachments, "entries": entries}
    NORMALIZED.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_canonical(normalized) + b"\n")
    return normalized


def _rebuild_database(documents: list[dict[str, Any]]) -> tuple[int, int]:
    tmp = DB_PATH.with_name(DB_PATH.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("""
        CREATE TABLE versions(
          ver_id INTEGER PRIMARY KEY, 효력일 TEXT NOT NULL CHECK(length(효력일) = 8),
          발령번호 TEXT NOT NULL, 발령일자 TEXT NOT NULL, 일련번호 TEXT NOT NULL UNIQUE,
          행정규칙명 TEXT NOT NULL);
        CREATE TABLE attachments(
          attachment_id INTEGER PRIMARY KEY, ver_id INTEGER NOT NULL REFERENCES versions(ver_id),
          ordinal INTEGER NOT NULL, source_url TEXT NOT NULL CHECK(instr(lower(source_url), 'law_oc') = 0),
          original_name TEXT NOT NULL, stored_name TEXT NOT NULL, format TEXT NOT NULL CHECK(format IN ('hwpx','hwp','pdf')),
          role TEXT NOT NULL CHECK(role IN ('annex','notice','qa','transition','reason','comparison','other')),
          size INTEGER NOT NULL CHECK(size >= 0), sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
          parser_version TEXT NOT NULL, parser_status TEXT NOT NULL CHECK(parser_status IN ('complete','failed','not_selected')),
          UNIQUE(ver_id, ordinal));
        CREATE TABLE entries(
          id INTEGER PRIMARY KEY, ver_id INTEGER NOT NULL REFERENCES versions(ver_id),
          attachment_id INTEGER NOT NULL REFERENCES attachments(attachment_id), action TEXT NOT NULL,
          class_no TEXT NOT NULL, class_header TEXT NOT NULL, title TEXT NOT NULL CHECK(length(title) > 0),
          norm_key TEXT NOT NULL, body TEXT NOT NULL, block_identity TEXT NOT NULL,
          UNIQUE(ver_id, attachment_id, block_identity));
        CREATE VIRTUAL TABLE fts USING fts5(title, body, content='entries', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
        """)
        versions = entries = 0
        for document in sorted(documents, key=lambda item: item["version"]["행정규칙일련번호"]):
            version = document["version"]
            cur = con.execute("INSERT INTO versions(효력일,발령번호,발령일자,일련번호,행정규칙명) VALUES(?,?,?,?,?)",
                              (version["시행일자"], version["발령번호"], version["발령일자"],
                               version["행정규칙일련번호"], version["행정규칙명"]))
            ver_id = cur.lastrowid
            versions += 1
            attachment_ids: dict[int, int] = {}
            for attachment in document["attachments"]:
                cur = con.execute("INSERT INTO attachments(ver_id,ordinal,source_url,original_name,stored_name,format,role,size,sha256,parser_version,parser_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                  (ver_id, attachment["ordinal"], attachment["source_url"], attachment["original_name"], attachment["stored_name"], attachment["format"], attachment["role"], attachment["size"], attachment["sha256"], attachment["parser_version"], attachment["parser_status"]))
                attachment_ids[attachment["ordinal"]] = cur.lastrowid
            for entry in document["entries"]:
                con.execute("INSERT INTO entries(ver_id,attachment_id,action,class_no,class_header,title,norm_key,body,block_identity) VALUES(?,?,?,?,?,?,?,?,?)",
                            (ver_id, attachment_ids[entry["attachment_ordinal"]], entry["action"], entry["class_no"], entry["class_header"], entry["title"], norm_title(entry["class_no"], entry["title"]), entry["body"], entry["block_identity"]))
                entries += 1
        con.execute("INSERT INTO fts(rowid,title,body) SELECT id,title,body FROM entries")
        con.commit()
    finally:
        con.close()
    os.replace(tmp, DB_PATH)
    return versions, entries


def main() -> None:
    documents = []
    for version_dir in sorted(RAW.iterdir()):
        meta_path = version_dir / "meta.json"
        if version_dir.is_dir() and meta_path.is_file():
            normalized = _parse_version(version_dir, _validate_meta(meta_path))
            normalized_path = NORMALIZED / f"{normalized['version']['행정규칙일련번호']}.json"
            documents.append(json.loads(normalized_path.read_text(encoding="utf-8")))
    versions, entries = _rebuild_database(documents)
    print(f"버전={versions}개 항목={entries}개 → {DB_PATH}")


if __name__ == "__main__":
    main()
