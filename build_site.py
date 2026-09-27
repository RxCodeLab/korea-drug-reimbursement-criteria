from __future__ import annotations

import collections
import hashlib
import json
import re
import shutil
import unicodedata
from urllib.parse import quote

from html import escape as html_escape

import mfds_match
from common import BASE, DATA
from fetch_mfds import CLASS_HEADER, term_groups_from_titles
from search import ACTION_LABELS, ROLE_ORDER

ASSETS = BASE / "assets"
# Static files deployed as-is (e.g. search-engine verification files).
SITE_FILES = BASE / "site"
NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
COLLECTION_STATUS_PATH = DATA / "collection.json"
PUBLIC = BASE / "public"
SITE_URL = "https://rxcodelab.github.io/korea-drug-reimbursement-criteria/"
SITE_NAME = "약제 급여기준 변경 이력 검색"
REPO_URL = "https://github.com/RxCodeLab/korea-drug-reimbursement-criteria"
DB_DOWNLOAD_URL = REPO_URL + "/releases/latest/download/criteria.db"
OG_IMAGE = "og.png"
FOOTER_LABEL_PLACEHOLDER = "__FOOTER_LABEL__"
MAX_BODY_CHARS = 50_000
# Fields of an MFDS product row, in order. The index is written column by column (encode_mfds_index).
MFDS_INDEX_FIELDS = (
    "item_seq", "item_name", "entp_name", "main_item_ingr", "main_item_ingr_eng",
    "permit_date", "revision_count", "content_key", "withdrawn",
)
# Low-cardinality columns hold integer ids into "dicts".
MFDS_INDEX_DICT_FIELDS = ("entp_name", "main_item_ingr", "main_item_ingr_eng", "permit_date", "withdrawn")
# Product search downloads only the blocks that can match a term (productResults in assets/search-core.js).
# Rows are clustered by ingredient so one drug's products share a few blocks.
MFDS_BLOCK_ROWS = 128
# Must equal GRAM_BUCKETS in assets/search-core.js.
MFDS_GRAM_BUCKETS = 256
PRODUCT_FIELDS = ("item_seq", "item_name", "main_item_ingr", "main_item_ingr_eng", "permit_date", "status", "cancel_date")
# HTML tags or unresolved entities left in ee_text by normalize_ee before v3.
EE_MARKUP_LEFTOVER = re.compile(r"<[a-zA-Z/][^>]*>|&[a-zA-Z#][a-zA-Z0-9]*;")


def load_normalized() -> list[dict]:
    if not NORMALIZED.is_dir():
        return []
    documents = []
    for path in sorted(NORMALIZED.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("complete") is not True:
            raise RuntimeError(f"incomplete normalized document: {path}")
        documents.append(document)
    return documents


def build_index(documents: list[dict], names: dict[str, dict] | None = None) -> list[dict]:
    names = names or {}
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
            aliases = name_aliases(names.get(entry["block_identity"])) if entry["class_no"] else ""
            if aliases:
                records[-1]["aliases"] = aliases
    return sorted(records, key=lambda r: (r["key"], r["effective_date"], r["sequence"], r["source_sha256"]))


# Fields the search page reads. Hashes and the other bookkeeping fields stay in the SQLite snapshot.
CRITERIA_INDEX_FIELDS = ("key", "title", "body", "action", "class_header", "effective_date", "notice_number",
                         "sequence", "source_name", "aliases")
DOCUMENT_INDEX_FIELDS = ("title", "body", "role", "ordinal", "effective_date", "notice_number", "sequence")


def browser_record(record: dict, fields: tuple[str, ...]) -> dict:
    slim = {field: record[field] for field in fields if field in record}
    if record["truncated"]:
        slim["truncated"] = True
    return slim


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def has_clean_current_text(record: dict) -> bool:
    """Every collected item is published unless its current text still has pre-v3 markup."""
    revisions = record.get("revisions") or []
    if not revisions:
        return False
    return not EE_MARKUP_LEFTOVER.search(str(revisions[0].get("ee_text") or ""))


def encode_mfds_index(rows: list[list[object]]) -> dict:
    """Column-oriented index: one array per field, which compresses better than rows.

    Product numbers become integers, and each current-indication hash becomes a small group id
    (the browser only compares them).
    """
    columns: dict[str, list[object]] = {field: [row[i] for row in rows] for i, field in enumerate(MFDS_INDEX_FIELDS)}
    dicts: dict[str, list[object]] = {}
    for field in MFDS_INDEX_DICT_FIELDS:
        ids: dict[object, int] = {}
        columns[field] = [ids.setdefault(value, len(ids)) for value in columns[field]]
        dicts[field] = list(ids)
    groups: dict[object, int] = {}
    columns["content_group"] = [groups.setdefault(value, len(groups)) for value in columns.pop("content_key")]
    columns["item_seq"] = [int(value) for value in columns["item_seq"]]
    return {**columns, "dicts": dicts}


def gram_bucket(gram: str) -> int:
    """Same formula as gramBucket in assets/search-core.js."""
    return (ord(gram[0]) * 65599 + ord(gram[1])) % MFDS_GRAM_BUCKETS


def row_texts(row: list[object]) -> list[str]:
    """The lowercased texts matchMfds searches: product name, ingredients, English ingredients, maker."""
    return [str(row[1]).lower(), str(row[3]).lower(), str(row[4]).lower(), str(row[2]).lower()]


def mfds_build_id(encoded: dict) -> str:
    layout = {"block_rows": MFDS_BLOCK_ROWS, "gram_buckets": MFDS_GRAM_BUCKETS}
    return hashlib.sha256(_compact_json({"layout": layout, "index": encoded}).encode("utf-8")).hexdigest()[:12]


def withdrawal_label(record: dict) -> str:
    """'2024-04-25 취하' for products no longer marketed; empty while marketed."""
    status = record.get("status") or ""
    if status in ("", "정상"):
        return ""
    return f"{iso_date(str(record.get('cancel_date') or ''))} {status}".strip()


def write_mfds_shards(output, rows: list[list[object]], encoded: dict, build: str) -> None:
    """Blocks of product rows plus, per two-character fragment, the blocks that contain it.

    Every block holding a term holds all of the term's fragments, so the blocks listed for all of them
    are a complete candidate set. Only fragments of letters and digits are listed; punctuation and spaces
    are not searched for. Each file carries the build id so the browser never mixes builds.
    """
    order = sorted(range(len(rows)), key=lambda r: (rows[r][3], r))
    grams: list[dict[str, set[int]]] = [collections.defaultdict(set) for _ in range(MFDS_GRAM_BUCKETS)]
    (output / "blocks").mkdir()
    for block, start in enumerate(range(0, len(order), MFDS_BLOCK_ROWS)):
        ids = order[start:start + MFDS_BLOCK_ROWS]
        payload = {"build": build, "row": ids,
                   **{field: [encoded[field][r] for r in ids] for field in ("item_seq", "item_name", "revision_count", "content_group")},
                   **{field: [rows[r][MFDS_INDEX_FIELDS.index(field)] for r in ids] for field in MFDS_INDEX_DICT_FIELDS}}
        (output / "blocks" / f"{block}.json").write_text(_compact_json(payload), encoding="utf-8")
        for r in ids:
            for text in row_texts(rows[r]):
                for i in range(len(text) - 1):
                    gram = text[i:i + 2]
                    if gram.isalnum():
                        grams[gram_bucket(gram)][gram].add(block)
    (output / "grams").mkdir()
    for bucket, table in enumerate(grams):
        payload = {"build": build, "grams": {gram: sorted(blocks) for gram, blocks in table.items()}}
        (output / "grams" / f"{bucket}.json").write_text(_compact_json(payload), encoding="utf-8")


def write_mfds_indexes(output, rows: list[list[object]]) -> str:
    """Full index plus search shards; returns the build id."""
    encoded = encode_mfds_index(rows)
    build = mfds_build_id(encoded)
    (output / "search-index.json").write_text(_compact_json({**encoded, "build": build}), encoding="utf-8")
    write_mfds_shards(output, rows, encoded, build)
    return build


def build_mfds_public(products: list[dict] | None = None) -> tuple[list[list[object]], str]:
    """Writes the MFDS search index and per-item JSON, regardless of reimbursement status.

    Returns the index rows and the build id. When given, products receives a summary of every published
    item for criteria_names().
    """
    rows: list[list[object]] = []
    output = PUBLIC / "mfds"
    items_dir = output / "items"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if not MFDS_ITEMS.is_dir():
        return rows, write_mfds_indexes(output, rows)
    items_dir.mkdir()
    for path in sorted(MFDS_ITEMS.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("complete") is not True:
            raise RuntimeError(f"incomplete MFDS item: {path}")
        if not has_clean_current_text(record):
            continue
        revisions = record.get("revisions") or []
        rows.append([
            record["item_seq"],
            record["item_name"],
            record["entp_name"],
            # Ingredient codes ([M258339]) are not shown or searched.
            "|".join(ingredient_parts(record["main_item_ingr"])),
            record.get("main_item_ingr_eng", ""),
            record["permit_date"],
            len(revisions),
            revisions[0]["content_sha256"] if revisions else "",
            withdrawal_label(record),
        ])
        if products is not None:
            products.append({key: record.get(key) or "" for key in PRODUCT_FIELDS})
        (items_dir / path.name).write_text(_compact_json(record), encoding="utf-8")
    rows.sort(key=lambda r: (r[1], r[0]))
    return rows, write_mfds_indexes(output, rows)


PRODUCT_DOSE = re.compile(
    r"[0-9][0-9./]*(?:밀리그램|밀리그람|그램|그람|밀리리터|리터|마이크로그램|mg|㎎|㎍|ml|㎖|g|iu|%|만단위|단위).*$", re.I
)
INGREDIENT_CODE = re.compile(r"^\s*\[M\d+\]")
HANGUL = re.compile(r"[가-힣]")
TITLE_NAME_MAX = 20
BRANDS_SHOWN = 3
TRAILING_QUALIFIER = re.compile(r"\s*\([^()]*\)$")
# Korean salt and hydrate suffixes, with the English words that name them in a criterion title. A suffix is
# dropped (펙수프라잔염산염 -> 펙수프라잔) unless the English title names it too (Dimethyl fumarate keeps 푸마르산염).
KOREAN_SALT_SUFFIXES = {
    "프로판디올수화물": ("propanediol", "hydrate"), "반수화물": ("hydrate",), "일수화물": ("hydrate",),
    "이수화물": ("hydrate",), "삼수화물": ("hydrate",), "수화물": ("hydrate",), "무수물": ("anhydrous",),
    "황산수소염": ("sulfate", "bisulfate"), "염산염": ("hydrochloride", "hcl"), "황산염": ("sulfate", "sulphate"),
    "시트르산염": ("citrate",), "메실산염": ("mesylate", "mesilate"), "말레산염": ("maleate",),
    "푸마르산염": ("fumarate",), "타르타르산염": ("tartrate",), "베실산염": ("besylate", "besilate"),
    "숙신산염": ("succinate",), "아세트산염": ("acetate",), "브롬화물": ("bromide",), "인산염": ("phosphate",),
    "탄산염": ("carbonate",), "이나트륨": ("sodium",), "디나트륨": ("sodium",), "나트륨": ("sodium",),
    "칼륨": ("potassium",), "칼슘": ("calcium",), "마그네슘": ("magnesium",),
}
# Dosage-form words inside ingredient names (란소프라졸과립, 클래리트로마이신제피과립).
KOREAN_FORM_SUFFIXES = ("제피과립", "장용과립", "과립", "펠렛")
# A second ingredient is named only when it covers this share of the matched products; below it,
# it is a spelling variant (시클로스포린) or a stray match.
SECOND_INGREDIENT_SHARE = 0.25
STRIPPED_SUFFIXES = (*KOREAN_FORM_SUFFIXES, *sorted(KOREAN_SALT_SUFFIXES, key=len, reverse=True))
# Product-name markers for the dosage forms that criteria titles name.
FORM_MARKERS = {
    "주사제": re.compile(r"주"),
    "경구제": re.compile(r"정|캡슐|시럽|과립|현탁|내용액|산$|환$"),
    "흡입제": re.compile(r"흡입|레스피맷|엘립타|디스커스|에어로|헬러"),
    "점안제": re.compile(r"점안"),
    "외용제": re.compile(r"점안|크림|연고|겔|로션|외용|패취|패치|스프레이|액$"),
}


def ingredient_parts(raw: str) -> list[str]:
    """Same rule as ingredientParts in assets/search-core.js."""
    parts = (INGREDIENT_CODE.sub("", part).strip() for part in str(raw).split("|"))
    return list(dict.fromkeys(part for part in parts if part))


def brand_name(item_name: str) -> str:
    """Same rule as brandName in assets/search-core.js, without lowercasing."""
    return PRODUCT_DOSE.sub("", item_name.split("(", 1)[0]).strip()


def base_ingredient(label: str, english: str = "") -> str:
    """Ingredient name without qualifiers, dosage-form words, and salts the English title does not name."""
    english_words = set(re.findall(r"[a-z]+", english.casefold()))
    label = TRAILING_QUALIFIER.sub("", label).strip() or label
    label = label.removeprefix("미분화").replace(" ", "") or label
    stripped = True
    while stripped:
        stripped = False
        for suffix in STRIPPED_SUFFIXES:
            rest = label[:-len(suffix)]
            if (label.endswith(suffix) and len(rest) >= 3 and HANGUL.search(rest)
                    and not english_words & set(KOREAN_SALT_SUFFIXES.get(suffix, ()))):
                label, stripped = rest, True
                break
    # Hydrate counts left behind, e.g. 오데빅시바트1.5(수화물).
    return re.sub(r"(?<=[가-힣])[\d.]+$", "", label)


def korean_ingredient(labels: collections.Counter, english: str = "") -> str:
    """The base name covering most matched products (a shorter base covers its salt variants)."""
    bases: collections.Counter = collections.Counter()
    for label, count in labels.items():
        # Products with no recorded ingredient have an empty label; it must not win.
        if label and " + " not in label:
            bases[base_ingredient(label, english)] += count
    if not bases:
        return next((label for label, _ in labels.most_common() if label), "")
    support = {base: sum(count for other, count in bases.items() if other.startswith(base)) for base in bases}
    ranked = sorted(support, key=lambda base: (-support[base], len(base)))
    named = [ranked[0]]
    total = sum(bases.values())
    for base in ranked[1:]:
        if support[base] >= total * SECOND_INGREDIENT_SHARE and not any(base.startswith(n) for n in named):
            named.append(base)
    return " · ".join(named)


def title_form(title: str) -> str:
    return next((form for form in FORM_MARKERS if form in title_head(title)), "")


def criteria_names(groups: list[list[dict]], products: list[dict]) -> dict[str, dict]:
    """Korean ingredient and product names per criterion, from the MFDS items its title matches.

    Uses the same title-to-item matching as MFDS collection (fetch_mfds, mfds_match).
    """
    if not products:
        return {}
    index = mfds_match.build_index([
        {"ITEM_SEQ": item["item_seq"], "ITEM_NAME": item["item_name"], "MAIN_INGR_ENG": item["main_item_ingr_eng"]}
        for item in products
    ])
    by_seq = {item["item_seq"]: item for item in products}
    names: dict[str, dict] = {}
    for items in groups:
        newest = items[0]
        terms = term_groups_from_titles([newest["title"]])
        # Titles listing several drugs, each with its own (품명 …), get no single Korean name.
        if not terms or newest["title"].count("(품명") > 1:
            continue
        # matched is a set; sorted so hash-seed order never decides ties in the chosen names.
        matched = [by_seq[seq] for seq in sorted(mfds_match.match_terms(index, terms).matched) if seq in by_seq]
        form = title_form(newest["title"])
        if form:
            matched = [item for item in matched if FORM_MARKERS[form].search(brand_name(item["item_name"]) or item["item_name"])] or matched
        if not matched:
            continue
        labels = collections.Counter(" + ".join(ingredient_parts(item["main_item_ingr"])) for item in matched)
        single = [item for item in matched if len(ingredient_parts(item["main_item_ingr"])) == 1] or matched
        # Earliest permit first, so the originator leads even when it has been withdrawn.
        statuses: dict[str, str] = {}
        for item in sorted(single, key=lambda item: (item["permit_date"] or "99999999", item["item_name"])):
            brand = brand_name(item["item_name"])
            if not brand:
                continue
            label = withdrawal_label(item)
            if brand not in statuses or not label:
                statuses[brand] = label
        # The originator is shown whatever its status; the rest only while marketed.
        ordered = list(statuses.items())
        brands = ordered[:1] + [(brand, state) for brand, state in ordered[1:] if not state]
        names[newest["identity"]] = {
            "ingredient": korean_ingredient(labels, title_head(newest["title"])),
            "brands": brands[:BRANDS_SHOWN],
        }
    return names


def name_aliases(names: dict | None) -> str:
    """Extra search text for a criterion; only names that its page also shows."""
    if not names:
        return ""
    return " ".join([names["ingredient"], *(brand for brand, _ in names["brands"])]).strip()


# Shared by the search page and the static criteria pages.
BASE_CSS = (
    ":root{--fg:#1d2433;--muted:#5b6575;--line:#d3d9e2;--soft:#f3f5f8;--bg:#fff;--accent:#1b5fc1;"
    "--new:#16703a;--new-bg:#e2f3e7;--chg:#34507a;--chg-bg:#e6ecf5;--del:#a3261f;--del-bg:#fbe5e3;color-scheme:light}"
    "@media(prefers-color-scheme:dark){:root{--fg:#e4e8ef;--muted:#9ba5b5;--line:#343d4b;--soft:#1a2029;--bg:#11151b;"
    "--accent:#7eb0ff;--new:#8fdca7;--new-bg:#16321f;--chg:#b5c8e8;--chg-bg:#1f2a3b;--del:#ffaaa3;--del-bg:#3d1c1a;"
    "color-scheme:dark}}"
    'body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;margin:0 auto;padding:2rem 1rem;'
    "line-height:1.55;color:var(--fg);background:var(--bg)}"
    "h1,h2,h3,summary{word-break:keep-all;overflow-wrap:anywhere;line-height:1.35}h1{font-size:clamp(1.4rem,5vw,1.9rem);margin:0 0 .4rem}"
    "a{color:var(--accent)}:focus-visible{outline:2px solid var(--accent);outline-offset:2px}[hidden]{display:none!important}"
    ".meta,.hint{color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--soft);padding:.8rem;border-radius:4px}"
    "footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid var(--line);color:var(--muted);font-size:.9rem}"
)


HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>약제 급여기준 변경 이력 검색</title>
__HEAD_META__
<style>
__BASE_CSS__
.hint{margin:0 0 .8rem}.bar{position:sticky;top:0;z-index:1;background:var(--bg);padding:.5rem 0 .1rem;border-bottom:1px solid var(--line)}
input{width:100%;box-sizing:border-box;padding:.8rem;font:inherit;color:inherit;background:var(--bg);border:1px solid var(--muted);border-radius:6px}input:focus{border-color:var(--accent)}
#status{margin:.45rem 0;font-size:.95rem}.intro{margin:1.5rem 0;color:var(--muted)}.examples{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.examples button{font:inherit;color:var(--fg);background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:.3rem .9rem;cursor:pointer}
.block{margin:1.8rem 0;scroll-margin-top:7rem}.block>h2,.related>summary>h2{font-size:1.2rem;margin:0 0 .4rem;padding-bottom:.35rem;border-bottom:2px solid var(--fg)}
.criterion{padding:.5rem 0 .9rem;border-bottom:1px solid var(--line)}.criterion:last-child{border-bottom:0}.criterion h3,.mfds h3{font-size:1.05rem;margin:.6rem 0 .2rem}.mfds h3{margin-top:1.3rem}.criterion>.meta{margin:0 0 .5rem;font-size:.92rem}
details{border:1px solid var(--line);border-radius:6px;margin:.45rem 0;padding:.5rem .8rem}summary{cursor:pointer;font-weight:600}.rev.latest{border-left:3px solid var(--accent)}
.older{border:0;padding:0}.older>summary{color:var(--accent);font-weight:500;padding:.2rem 0}.older>.rev{margin-left:1rem}
.badge{display:inline-block;padding:0 .5rem;border-radius:999px;font-size:.8rem;font-weight:600;line-height:1.7;color:var(--muted);background:var(--soft)}
.badge[data-kind="신설"]{color:var(--new);background:var(--new-bg)}.badge[data-kind="변경"]{color:var(--chg);background:var(--chg-bg)}.badge[data-kind="삭제"]{color:var(--del);background:var(--del-bg)}.badge[data-kind="latest"]{color:var(--bg);background:var(--accent)}
.count{color:var(--muted);font-weight:400}.nw{white-space:nowrap}.revision{font-weight:700;margin:.8rem 0 .2rem}.empty{padding:2rem 0;color:var(--muted)}
.more{display:block;width:100%;margin:.8rem 0;padding:.7rem;font:inherit;color:var(--fg);background:var(--soft);border:1px solid var(--line);border-radius:6px;cursor:pointer}.more:hover,.examples button:hover{border-color:var(--muted)}
.related{border:0;padding:0}.related>summary{list-style:none}.related>summary::-webkit-details-marker{display:none}.related>summary>h2::after{content:" ▸";color:var(--muted)}.related[open]>summary>h2::after{content:" ▾"}
.catalog{margin-top:2rem;color:var(--muted);font-size:.9rem}.catalog h3{font-size:.95rem;margin:1rem 0 .3rem;color:var(--fg)}.catalog ul{columns:2;margin:.3rem 0;padding-left:1.2rem}@media(max-width:600px){.catalog ul{columns:1}}
</style></head><body>
<h1>약제 급여기준 변경 이력 검색</h1><p class="hint">보건복지부 약제 급여기준의 신설·변경·삭제 이력과 식약처 허가 적응증을 찾습니다.</p>
<div class="bar"><input id="q" type="search" autocomplete="off" placeholder="성분명·제품명 (예: 다파글리플로진, dapagliflozin)" aria-label="검색어" autofocus><p id="status" class="meta" aria-live="polite">급여기준 색인 불러오는 중…</p></div>
__INTRO__<main id="results"></main>
__DRUG_CATALOG__
<footer>__FOOTER_LABEL__<a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">데이터 수집·검증 과정 보기</a></footer>
<script>
__SEARCH_JS__
</script></body></html>'''


def latest_notice_label(documents: list[dict]) -> str:
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
    """Last successful collection date only; never expose failures or attempts."""
    if not COLLECTION_STATUS_PATH.is_file():
        return ""
    try:
        status = json.loads(COLLECTION_STATUS_PATH.read_text(encoding="utf-8"))
        raw_date = str(status["last_success_date"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise RuntimeError(f"malformed {COLLECTION_STATUS_PATH}") from None
    if not raw_date:
        return ""
    date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return f"마지막 수집 성공: {date} · "


SLUG_STRIP = re.compile("[^0-9a-z가-힣]+")


def slugify(title: str) -> str:
    slug = SLUG_STRIP.sub("-", title.casefold()).strip("-")
    return slug[:80].rstrip("-") or "criteria"


def identity_slug(block_identity: str) -> str:
    """Slug from block_identity, so URLs survive title changes.

    NFKC keeps identities differing only by Roman numerals (Ⅷ/Ⅸ) from collapsing into one slug.
    """
    return slugify(unicodedata.normalize("NFKC", block_identity).replace("[", "").replace("]", "-"))


def page_slugs(identities: list[str]) -> dict[str, str]:
    """Colliding slugs all get a hash suffix, so URLs do not depend on ordering."""
    base = {identity: identity_slug(identity) for identity in identities}
    counts: dict[str, int] = {}
    for slug in base.values():
        counts[slug] = counts.get(slug, 0) + 1
    return {
        identity: slug if counts[slug] == 1 else f"{slug}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:6]}"
        for identity, slug in base.items()
    }


def criteria_groups(documents: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for document in documents:
        version = document["version"]
        for entry in document["entries"]:
            if not entry["class_no"]:
                continue
            groups.setdefault(entry["block_identity"], []).append({
                "identity": entry["block_identity"],
                "title": entry["title"],
                "class_no": entry["class_no"],
                "class_header": entry["class_header"],
                "action": entry["action"],
                "body": entry["body"],
                "effective_date": version["시행일자"],
                "notice_date": str(version.get("발령일자") or ""),
                "notice_number": version["발령번호"],
                "sequence": version["행정규칙일련번호"],
            })
    ordered = [
        sorted(items, key=lambda r: (r["effective_date"], r["sequence"]), reverse=True)
        for items in groups.values()
    ]
    ordered.sort(key=lambda items: items[0]["title"].casefold())
    return ordered


def iso_date(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) >= 8 else ""


def json_ld(value: dict) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>'


def social_meta(title: str, description: str, url: str, kind: str) -> list[str]:
    tags = [
        f'<meta property="og:type" content="{kind}">',
        f'<meta property="og:site_name" content="{SITE_NAME}">',
        '<meta property="og:locale" content="ko_KR">',
        f'<meta property="og:title" content="{html_escape(title)}">',
        f'<meta property="og:description" content="{html_escape(description)}">',
        f'<meta property="og:url" content="{html_escape(url)}">',
    ]
    if (ASSETS / OG_IMAGE).is_file():
        tags += [
            f'<meta property="og:image" content="{SITE_URL}{OG_IMAGE}">',
            '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
    return tags


def title_head(title: str) -> str:
    """The title without the class header and the '(품명 …)' example list."""
    return CLASS_HEADER.sub("", title.split("(품명", 1)[0]).strip()


def korean_suffix(names: dict | None) -> str:
    """' (다파글리플로진)': the 식약처 ingredient name, added after the notice's own title, never in place of it."""
    korean = (names or {}).get("ingredient", "")
    if not korean or len(korean) > TITLE_NAME_MAX or not HANGUL.search(korean):
        return ""
    return f" ({korean})"


def page_heading(title_text: str, names: dict | None) -> str:
    return f"{title_text} 급여기준 변경 이력{korean_suffix(names)}"


def page_description(items: list[dict], names: dict | None) -> str:
    newest = items[0]
    name = (title_head(newest["title"]) or newest["title"]) + korean_suffix(names)
    text = (f"{name} 급여기준. 최근 개정 {iso_date(newest['effective_date'])} 시행"
            f"(고시 제{newest['notice_number']}호), 개정 이력 {len(items)}건.")
    if names and names["brands"]:
        text += f" 허가 품목: {', '.join(brand for brand, _ in names['brands'])} 등."
    text += " " + " ".join(newest["body"].split())
    return text if len(text) <= 160 else text[:159].rstrip() + "…"


def criteria_page(newest: dict, items: list[dict], url_path: str, names: dict | None = None,
                  related: list[tuple[str, str]] = (), siblings: list[tuple[str, str]] = (),
                  heading: str | None = None, class_label: str | None = None) -> str:
    title = newest["title"]
    heading = heading or page_heading(title_head(title) or title, names)
    description = page_description(items, names)
    canonical = SITE_URL + url_path
    modified = iso_date(max(record["notice_date"] for record in items))
    graph = [
        {"@type": "WebPage", "@id": canonical, "url": canonical, "name": heading, "description": description,
         "inLanguage": "ko", "isPartOf": {"@id": SITE_URL + "#website"},
         **({"dateModified": modified} if modified else {})},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": SITE_URL},
            {"@type": "ListItem", "position": 2, "name": title, "item": canonical},
        ]},
    ]
    parts = [
        '<!doctype html>',
        '<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html_escape(heading)}</title>",
        f'<meta name="description" content="{html_escape(description)}">',
        '<meta name="robots" content="index,follow">',
        *social_meta(heading, description, canonical, "article"),
        f'<link rel="canonical" href="{html_escape(canonical)}">',
        json_ld({"@context": "https://schema.org", "@graph": graph}),
        f"<style>{BASE_CSS}article{{border:1px solid var(--line);border-radius:6px;margin:1rem 0;padding:.8rem}}"
        "article h2{font-size:1.05rem;margin:.2rem 0}.crumbs{font-size:.92rem}"
        ".siblings ul{columns:2;padding-left:1.2rem}@media(max-width:600px){.siblings ul{columns:1}}</style></head><body>",
        f'<nav class="crumbs meta"><a href="../">{SITE_NAME}</a> › '
        f'{html_escape(class_label or newest["class_header"] or "[" + newest["class_no"] + "]")}</nav>',
        f"<h1>{html_escape(title)}</h1>",
    ]
    facts = [f"최근 개정 {iso_date(newest['effective_date'])} 시행", f"개정 이력 {len(items)}건"]
    parts.append(f'<p class="meta">{" · ".join(facts)}</p>')
    if names:
        brands = ", ".join(html_escape(brand + (f"({state})" if state else "")) for brand, state in names["brands"])
        # The search box takes one phrase: combinations use their first English name, others the first Korean name.
        query = (re.findall(r"[A-Za-z0-9]+", title_head(title))[0] if " + " in names["ingredient"]
                 else names["ingredient"].split(" · ")[0])
        parts.append(
            f'<p class="meta">식약처 허가 성분명: {html_escape(names["ingredient"])}'
            + (f" · 허가 품목: {brands} 등" if brands else "")
            + f' · <a href="../?q={quote(query)}">품목 전체 보기</a></p>'
        )
    if related:
        links = ", ".join(f'<a href="{path}">{html_escape(label)}</a>' for label, path in related)
        parts.append(f'<p class="meta">함께 적용되는 일반원칙: {links}</p>')
    for record in items:
        date = record["effective_date"]
        notice_url = ("https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq="
                      + quote(str(record["sequence"])))
        parts.append(
            "<article>"
            f"<h2>{iso_date(date)} 시행 · "
            f"고시 제{html_escape(record['notice_number'])}호 · {html_escape(record['action'])}</h2>"
            f'<p class="meta"><a href="{notice_url}" target="_blank" rel="noopener">'
            "국가법령정보센터 원문</a></p>"
            f"<pre>{html_escape(record['body'])}</pre></article>"
        )
    if siblings:
        links = "".join(f'<li><a href="{path}">{html_escape(label)}</a></li>' for label, path in siblings)
        parts.append(f'<section class="siblings"><h2>같은 분류의 급여기준</h2><ul>{links}</ul></section>')
    parts.append("<footer>" + FOOTER_LABEL_PLACEHOLDER + '<a href="'
                 + REPO_URL + '">데이터 수집·검증 과정 보기</a></footer></body></html>')
    return chr(10).join(parts)


GENERAL_CLASS = "[일반원칙]"


def class_heading(entries: list[dict]) -> str:
    """One heading per class number; the same number is worded differently across notices."""
    class_no = entries[0]["class_no"]
    if class_no == "일반원칙":
        return "일반원칙"
    headers = collections.Counter(entry["class_header"] for entry in entries
                                  if entry["class_header"].startswith(f"[{class_no}]"))
    return headers.most_common(1)[0][0] if headers else f"[{class_no}]"


def unique_titles(groups: list[list[dict]], names: dict[str, dict]) -> dict[str, str]:
    """Page headings that differ between pages: the notice's title without its product list, then the full
    title, then the full title with the class number."""
    choices = [
        lambda items: title_head(items[0]["title"]) or items[0]["title"],
        lambda items: items[0]["title"],
        lambda items: f'{items[0]["title"]} [{items[0]["class_no"]}]',
    ]
    titles: dict[str, str] = {}
    pending = groups
    for choose in choices:
        heading = lambda items: page_heading(choose(items), names.get(items[0]["identity"]))
        counts = collections.Counter(heading(items) for items in pending)
        taken = set(titles.values())
        for items in pending:
            title = heading(items)
            if (counts[title] == 1 and title not in taken) or choose is choices[-1]:
                titles[items[0]["identity"]] = title
        pending = [items for items in pending if items[0]["identity"] not in titles]
    return titles


def build_criteria_pages(groups: list[list[dict]], footer_label: str,
                         names: dict[str, dict] | None = None) -> list[dict]:
    """Writes one page per criterion and returns catalog entries (title, path, class_header, class_no, lastmod)."""
    names = names or {}
    output = PUBLIC / "criteria"
    if output.exists():
        shutil.rmtree(output)
    slugs = page_slugs([items[0]["identity"] for items in groups])
    titles = unique_titles(groups, names)
    entries = [{
        "identity": items[0]["identity"],
        "title": items[0]["title"],
        "path": f"criteria/{quote(slugs[items[0]['identity']])}.html",
        "class_no": items[0]["class_no"],
        "class_header": items[0]["class_header"],
        "lastmod": iso_date(max(record["notice_date"] for record in items)),
    } for items in groups]
    by_class: dict[str, list[dict]] = collections.defaultdict(list)
    for entry in entries:
        by_class[entry["class_no"]].append(entry)
    general = by_class.get("일반원칙", [])
    own = lambda other: other["path"].removeprefix("criteria/")
    for items, entry in zip(groups, entries):
        newest = items[0]
        # A criterion cites the general principles it depends on as '[일반원칙] 이름'.
        body = " ".join(newest["body"].split())
        related = [(other["title"], own(other)) for other in general
                   if other is not entry and f"{GENERAL_CLASS} {' '.join(other['title'].split())}" in body]
        siblings = [(other["title"], own(other)) for other in by_class[entry["class_no"]] if other is not entry]
        page = criteria_page(newest, items, entry["path"], names.get(entry["identity"]), related, siblings,
                             titles[entry["identity"]], class_heading(by_class[entry["class_no"]]))
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{slugs[entry['identity']]}.html").write_text(
            page.replace(FOOTER_LABEL_PLACEHOLDER, footer_label) + chr(10), encoding="utf-8")
    return entries


def class_sort_key(class_no: str) -> tuple:
    return (0, 0) if not class_no.isdigit() else (1, int(class_no))


def static_drug_list(catalog: list[dict]) -> str:
    """Static list of criteria pages grouped by class, so crawlers can reach them without JS."""
    if not catalog:
        return ""
    by_class: dict[str, list[dict]] = collections.defaultdict(list)
    for entry in catalog:
        by_class[entry["class_no"]].append(entry)
    sections = "".join(
        f"<h3>{html_escape(class_heading(by_class[class_no]))}</h3><ul>"
        + "".join(f'<li><a href="{entry["path"]}">{html_escape(entry["title"])}</a></li>' for entry in by_class[class_no])
        + "</ul>"
        for class_no in sorted(by_class, key=class_sort_key)
    )
    return (f'<details class="catalog"><summary>수록된 약제 급여기준 {len(catalog):,}건 목록</summary>'
            f"{sections}</details>")


def write_crawler_files(pages: list[tuple[str, str]]) -> None:
    """Takes (url, lastmod) pairs. robots.txt has no effect on a project page; it only documents the sitemap."""
    urls = "".join(
        f"<url><loc>{html_escape(url)}</loc>" + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "") + "</url>"
        for url, lastmod in pages
    )
    sitemap_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        "",
    ]
    (PUBLIC / "sitemap.xml").write_text(chr(10).join(sitemap_lines), encoding="utf-8")
    robots_lines = ["User-agent: *", "Allow: /", "", f"Sitemap: {SITE_URL}sitemap.xml", ""]
    (PUBLIC / "robots.txt").write_text(chr(10).join(robots_lines), encoding="utf-8")


def copy_static_files() -> None:
    if (ASSETS / OG_IMAGE).is_file():
        shutil.copyfile(ASSETS / OG_IMAGE, PUBLIC / OG_IMAGE)
    if not SITE_FILES.is_dir():
        return
    for source in SITE_FILES.rglob("*"):
        if source.is_file() and source.name != "README.txt":
            target = PUBLIC / source.relative_to(SITE_FILES)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def search_script() -> str:
    """Inlined to avoid extra requests. Core must come first: ui destructures SearchCore."""
    return "\n".join((ASSETS / name).read_text(encoding="utf-8").strip()
                     for name in ("search-core.js", "search-ui.js"))


def intro_html(latest_effective: str) -> str:
    examples = [("다파글리플로진", "다파글리플로진"), ("자렐토", "자렐토")]
    if latest_effective:
        date = iso_date(latest_effective)
        examples.append((date, f"{date} 시행분"))
    buttons = "".join(
        f'<button type="button" data-q="{html_escape(query)}">{html_escape(label)}</button>' for query, label in examples
    )
    return ('<section id="intro" class="intro"><p>성분명(한글·영문), 제품명, 시행일(YYYY-MM-DD)로 찾을 수 있습니다.</p>'
            f'<p class="examples">예시 {buttons}</p></section>')


INDEX_DESCRIPTION = ("보건복지부 약제 급여기준의 신설·변경·삭제 이력과 시행일·고시번호를 성분명(한글·영문)과 "
                     "제품명으로 검색합니다. 식약처 허가 적응증도 함께 보여줍니다.")


def index_head(latest_notice: str) -> str:
    website = {"@type": "WebSite", "@id": SITE_URL + "#website", "name": SITE_NAME, "url": SITE_URL,
               "description": INDEX_DESCRIPTION, "inLanguage": "ko"}
    dataset = {
        "@type": "Dataset", "name": "약제 급여기준 변경 이력 데이터", "url": SITE_URL, "inLanguage": "ko",
        "description": ("보건복지부 고시 「요양급여의 적용기준 및 방법에 관한 세부사항(약제)」의 시행일별 급여기준 "
                        "원문과 신설·변경·삭제 이력. 국가법령정보센터 Open API에서 수집해 검증한 SQLite 파일입니다."),
        "isBasedOn": "https://www.law.go.kr/",
        "distribution": {"@type": "DataDownload", "encodingFormat": "application/vnd.sqlite3", "contentUrl": DB_DOWNLOAD_URL},
        **({"dateModified": latest_notice} if latest_notice else {}),
    }
    return chr(10).join([
        f'<meta name="description" content="{INDEX_DESCRIPTION}">',
        '<meta name="robots" content="index,follow">',
        *social_meta(SITE_NAME, INDEX_DESCRIPTION, SITE_URL, "website"),
        f'<link rel="canonical" href="{SITE_URL}">',
        json_ld({"@context": "https://schema.org", "@graph": [website, dataset]}),
    ])


def latest_notice_date(documents: list[dict]) -> str:
    return iso_date(max((str(document["version"].get("발령일자") or "") for document in documents), default=""))


def latest_effective_date(documents: list[dict]) -> str:
    return max((str(document["version"]["시행일자"]) for document in documents), default="")


def render_index_page(catalog: list[dict], footer_label: str, latest_effective: str = "", latest_notice: str = "",
                      mfds_build: str = "") -> str:
    role_ranks = {role: rank for rank, role in enumerate(ROLE_ORDER)}
    return (
        HTML.replace("__HEAD_META__", index_head(latest_notice))
        .replace("__BASE_CSS__", BASE_CSS)
        .replace("__INTRO__", intro_html(latest_effective))
        .replace("__SEARCH_JS__", search_script())
        .replace("__ACTION_LABELS__", json.dumps(ACTION_LABELS, ensure_ascii=False, separators=(",", ":")))
        .replace("__ROLE_RANKS__", json.dumps(role_ranks, ensure_ascii=False, separators=(",", ":")))
        .replace("__MFDS_BUILD__", json.dumps(mfds_build))
        .replace(FOOTER_LABEL_PLACEHOLDER, footer_label)
        .replace("__DRUG_CATALOG__", static_drug_list(catalog))
    )


def main() -> None:
    if PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    PUBLIC.mkdir()
    copy_static_files()
    documents = load_normalized()
    products: list[dict] = []
    mfds_rows, mfds_build = build_mfds_public(products)
    groups = criteria_groups(documents)
    names = criteria_names(groups, products)
    index = build_index(documents, names)
    # Criteria load first; notices and attachments are fetched after them (assets/search-ui.js).
    criteria_rows = [browser_record(row, CRITERIA_INDEX_FIELDS) for row in index if row["class_no"]]
    document_rows = [browser_record(row, DOCUMENT_INDEX_FIELDS) for row in index if not row["class_no"]]
    (PUBLIC / "search-index.json").write_text(_compact_json(criteria_rows), encoding="utf-8")
    (PUBLIC / "documents-index.json").write_text(_compact_json(document_rows), encoding="utf-8")
    footer_label = latest_notice_label(documents) + last_success_label()
    catalog = build_criteria_pages(groups, footer_label, names)
    latest_notice = latest_notice_date(documents)
    write_crawler_files([(SITE_URL, latest_notice)] + [(SITE_URL + entry["path"], entry["lastmod"]) for entry in catalog])
    page = render_index_page(catalog, footer_label, latest_effective_date(documents), latest_notice, mfds_build)
    (PUBLIC / "index.html").write_text(page + "\n", encoding="utf-8")
    print(f"wrote {len(index)} search records, {len(mfds_rows)} MFDS items and "
          f"{len(catalog)} criteria pages ({len(names)} with Korean names) to {PUBLIC}")


if __name__ == "__main__":
    main()
