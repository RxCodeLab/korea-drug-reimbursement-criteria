from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

PARSER_VERSION = "documents-2"
_MAX_ZIP_MEMBERS = 256
_MAX_SECTION_BYTES = 16 * 1024 * 1024
_MAX_HWP_OUTPUT = 32 * 1024 * 1024
_SECTION = re.compile(r"Contents/section(\d+)\.xml\Z")


class ExtractionError(RuntimeError):
    pass


def extract_hwpx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_MEMBERS:
                raise ExtractionError("HWPX에 ZIP 항목이 너무 많습니다")
            sections: list[tuple[int, zipfile.ZipInfo]] = []
            for info in infos:
                if info.flag_bits & 1:
                    raise ExtractionError("HWPX에 암호화된 ZIP 항목이 포함되어 있습니다")
                match = _SECTION.fullmatch(info.filename)
                if match:
                    if info.file_size > _MAX_SECTION_BYTES:
                        raise ExtractionError(f"HWPX 섹션이 {_MAX_SECTION_BYTES}바이트 제한을 초과했습니다")
                    sections.append((int(match.group(1)), info))
            if not sections:
                raise ExtractionError("HWPX에 번호가 있는 Contents/sectionN.xml 항목이 없습니다")
            if len({number for number, _ in sections}) != len(sections):
                raise ExtractionError("HWPX에 중복된 섹션 번호가 있습니다")

            paragraphs: list[str] = []
            for _, info in sorted(sections, key=lambda pair: pair[0]):
                with archive.open(info) as member:
                    xml = member.read(_MAX_SECTION_BYTES + 1)
                if len(xml) > _MAX_SECTION_BYTES:
                    raise ExtractionError("HWPX 섹션이 읽기 제한을 초과했습니다")
                try:
                    root = ElementTree.fromstring(xml)
                except ElementTree.ParseError as exc:
                    raise ExtractionError(f"HWPX XML이 잘못되었습니다: {info.filename}") from exc
                for paragraph in root.iter():
                    if not paragraph.tag.endswith("}p"):
                        continue
                    text = "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t")).strip()
                    if text:
                        paragraphs.append(text)
            return "\n".join(paragraphs)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExtractionError(f"HWPX ZIP 파일이 잘못되었습니다: {path.name}") from exc


def extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:
        raise ExtractionError(f"PDF 텍스트 추출 실패: {path.name}: {exc}") from exc


def extract_hwp(path: Path) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="hwp-extract-") as output:
            subprocess.run(
                ["hwp5html", "--output", output, str(path)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            xhtml = Path(output) / "index.xhtml"
            if not xhtml.is_file() or xhtml.stat().st_size > _MAX_HWP_OUTPUT:
                raise ExtractionError("구형 HWP HTML 출력이 없거나 용량 제한을 초과했습니다")
            try:
                root = ElementTree.parse(xhtml).getroot()
            except ElementTree.ParseError as exc:
                raise ExtractionError("구형 HWP HTML 출력이 잘못되었습니다") from exc
            paragraphs = [
                "".join(node.itertext()).strip()
                for node in root.iter()
                if node.tag.endswith("}p") and "".join(node.itertext()).strip()
            ]
            return "\n".join(paragraphs)
    except FileNotFoundError as exc:
        raise ExtractionError("구형 HWP를 처리하려면 hwp5html 명령(pyhwp)이 설치되어 있어야 합니다") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError(f"구형 HWP 텍스트 추출 실패: {path.name}: {exc}") from exc


def extract_document(path: Path, document_format: str) -> str:
    dispatch = {"hwpx": extract_hwpx, "hwp": extract_hwp, "pdf": extract_pdf}
    try:
        return dispatch[document_format.lower()](path)
    except KeyError as exc:
        raise ExtractionError(f"지원하지 않는 문서 형식입니다: {document_format}") from exc
