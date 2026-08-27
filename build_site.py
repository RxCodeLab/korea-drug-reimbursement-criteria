from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote

from html import escape as html_escape

from common import BASE, DATA

NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
PUBLIC = BASE / "public"
SITE_URL = "https://rxcodelab.github.io/korea-drug-reimbursement-criteria/"
REPO_URL = "https://github.com/RxCodeLab/korea-drug-reimbursement-criteria"
FOOTER_LABEL_PLACEHOLDER = "__LATEST_NOTICE__"
MAX_BODY_CHARS = 50_000

def build_index() -> list[dict]:
    records: list[dict] = []
    if not NORMALIZED.is_dir():
        return records
    for path in sorted(NORMALIZED.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("complete") is not True:
            raise RuntimeError(f"완료되지 않은 정규화 문서입니다: {path}")
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


def build_mfds_public() -> list[dict]:
    index: list[dict] = []
    output = PUBLIC / "mfds"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if not MFDS_ITEMS.is_dir():
        (output / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
        return index
    items_dir = output / "items"
    for path in sorted(MFDS_ITEMS.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("complete") is not True:
            raise RuntimeError(f"완료되지 않은 MFDS 항목입니다: {path}")
        revisions = record.get("revisions") or []
        index.append({
            "item_seq": record["item_seq"],
            "item_name": record["item_name"],
            "entp_name": record["entp_name"],
            "main_item_ingr": record["main_item_ingr"],
            "main_item_ingr_eng": record.get("main_item_ingr_eng", ""),
            "status": record["status"],
            "permit_date": record["permit_date"],
            "revision_count": len(revisions),
            "current_content_sha256": revisions[0]["content_sha256"] if revisions else "",
            "last_observed_at": revisions[0]["last_observed_at"] if revisions else "",
            "source_url": record["source_url"],
        })
        target = items_dir / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_compact_json(record), encoding="utf-8")
    index.sort(key=lambda r: (r["item_name"], r["item_seq"]))
    (output / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
    return index


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
body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1d2433}input{width:100%;box-sizing:border-box;padding:.8rem;font-size:1rem;border:1px solid #8993a4;border-radius:6px}.hint,.meta{color:#5b6575}.group{margin:1.5rem 0;border-top:2px solid #28364d}.group h2{font-size:1.15rem}details{border:1px solid #ccd2dc;border-radius:6px;margin:.5rem 0;padding:.5rem .8rem}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:.8rem}.badge{color:#a33;margin-left:.5rem}.revision{font-weight:700;margin:.6rem 0 .2rem}.empty{padding:2rem 0;color:#5b6575}.related{margin-top:2rem;border-color:#8993a4}.related>.group{margin-left:.5rem}.catalog{margin-top:2rem;color:#5b6575;font-size:.9rem}.catalog ul{columns:2;margin:.5rem 0;padding-left:1.2rem}footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid #ccd2dc;color:#5b6575;font-size:.9rem}
</style></head><body>
<h1>약제 급여기준 변경 이력 검색</h1><p class="hint">한글 또는 영문 검색어를 공백으로 나누어 입력하면, 하나라도 포함된 항목을 표시합니다.</p>
<input id="q" type="search" autocomplete="off" placeholder="예: dapagliflozin 다파글리플로진" autofocus><p id="status" class="meta"></p><main id="results"></main>
__DRUG_CATALOG__
<footer>__LATEST_NOTICE__<a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">데이터 수집·검증 과정 보기</a></footer>
<script>
const q=document.querySelector('#q'),status=document.querySelector('#status'),root=document.querySelector('#results');let rows=[],mfdsRows=null;
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const actionLabels={notice:'고시문',comparison:'변경대비표',reason:'개정이유',qa:'질의응답',transition:'경과규정',other:'기타자료',annex:'별지','':'기준'};
const actionLabel=action=>actionLabels[action]??action;
const roleRanks={notice:0,reason:1,comparison:2,annex:3,transition:4,qa:5,other:6};
const roleRank=role=>roleRanks[role]??7;
const dateLabel=date=>`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6)}`;
const versionHeader=record=>`${dateLabel(record.effective_date)} 시행 · 고시 제${record.notice_number}호`;
const noticeLink=seq=>{const a=el('a','국가법령정보센터 원문');a.href=`https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq=${encodeURIComponent(seq)}`;a.target='_blank';a.rel='noopener';return a};
const byNewest=(a,b)=>b.effective_date.localeCompare(a.effective_date)||b.sequence.localeCompare(a.sequence);
const byRole=(a,b)=>roleRank(a.role)-roleRank(b.role)||a.ordinal-b.ordinal;
const baseName=name=>name.replace(/[(].*$/,'');
const brandName=name=>baseName(name).replace(/[0-9][0-9./]*(밀리그램|밀리그람|그램|그람|밀리리터|리터|마이크로그램|mg|㎎|ml|㎖|g|iu|%|만단위|단위).*$/i,'').trim().toLocaleLowerCase('ko');
const mfdsTier=(item,terms)=>{const brand=brandName(item.item_name),name=item.item_name.toLocaleLowerCase('ko'),exact=(item.entp_name+'|'+item.main_item_ingr+'|'+item.main_item_ingr_eng).toLocaleLowerCase('ko');if(terms.some(t=>brand===t))return 0;if(terms.some(t=>brand.startsWith(t)))return 1;if(terms.some(t=>name.includes(t)))return 2;if(terms.some(t=>exact.includes(t)))return 3;return 4};
const distinctClassHeader=record=>{const header=record.class_header.replace(/^\[[^\]]+\]\s*/,'').replace(/\s+/g,''),title=record.title.replace(/\s+/g,'');return header&&!header.startsWith(title)&&!title.startsWith(header)};
const TRUNCATED='\n\n[검색 색인에는 본문 일부만 표시됩니다. 전체 내용은 DB에서 확인하세요.]';
function groupsBy(records,field){const groups=new Map();for(const record of records){if(!groups.has(record[field]))groups.set(record[field],[]);groups.get(record[field]).push(record)}return[...groups.values()]}
function groupCriteria(records){return groupsBy(records,'key').map(items=>items.sort(byNewest)).sort((a,b)=>byNewest(a[0],b[0]))}
function groupDocuments(records){return groupsBy(records,'sequence').map(items=>items.sort(byRole)).sort((a,b)=>byNewest(a[0],b[0]))}
function appendCriteria(groups,container){for(const items of groups){const section=el('section',undefined,'group'),newest=items[0];section.append(el('h2',newest.title));if(distinctClassHeader(newest))section.append(el('p',newest.class_header,'meta'));for(const record of items){const details=el('details'),summary=el('summary');summary.append(document.createTextNode(versionHeader(record)));summary.append(el('span',actionLabel(record.action),'badge'));details.append(summary);const source=el('p',`출처: ${record.source_name} · `,'meta');source.append(noticeLink(record.sequence));details.append(source);details.append(el('pre',record.body+(record.truncated?TRUNCATED:'')));section.append(details)}container.append(section)}}
function appendDocuments(groups,container){for(const items of groups){const section=el('section',undefined,'group'),head=el('p',undefined,'revision');head.append(document.createTextNode(versionHeader(items[0])));for(const role of[...new Set(items.map(item=>item.role))].sort((a,b)=>roleRank(a)-roleRank(b)))head.append(el('span',actionLabel(role),'badge'));head.append(document.createTextNode(' · '));head.append(noticeLink(items[0].sequence));section.append(head);for(const record of items){const details=el('details'),summary=el('summary');summary.append(el('span',actionLabel(record.role),'badge'));summary.append(document.createTextNode(` ${record.source_name}`));details.append(summary);details.append(el('pre',record.body+(record.truncated?TRUNCATED:'')));section.append(details)}container.append(section)}}
function indicationGroups(items){
  const ingredients=groupsBy(items,'main_item_ingr');
  return ingredients.map(products=>({
    ingredient:products[0].main_item_ingr||'성분 미상',
    groups:groupsBy(products,'current_content_sha256').map(group=>group.sort((a,b)=>{
      const aDate=a.permit_date||'99999999',bDate=b.permit_date||'99999999';
      return aDate.localeCompare(bDate)||a.item_name.localeCompare(b.item_name);
    }))
  }));
}
function loadMfdsItem(item,target,currentOnly){
  target.textContent='불러오는 중…';
  return fetch(`mfds/items/${item.item_seq}.json`).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(doc=>{
    const revisions=doc.revisions||[];
    if(!revisions.length){target.textContent='효능·효과 정보가 없습니다.';return;}
    if(currentOnly){target.textContent=revisions[0].ee_text;return;}
    target.textContent=revisions.map((revision,index)=>{
      const label=index===0?'현재':'이전';
      const date=revision.official_revision_date?`허가사항 변경일 ${revision.official_revision_date}`:`최초 관찰 ${revision.first_observed_at.slice(0,10)} · 최종 관찰 ${revision.last_observed_at.slice(0,10)}`;
      return `[${label} · ${date}]\n${revision.ee_text}`;
    }).join('\n\n');
  }).catch(()=>{target.textContent='상세 정보를 불러오지 못했습니다.'});
}
function appendMfds(items,container,terms){
  const section=el('section',undefined,'group mfds');
  section.append(el('h2','식약처 허가 적응증'));
  section.append(el('p','효능·효과가 같은 품목은 함께 표시합니다.','meta'));
  for(const ingredient of indicationGroups(items)){
    section.append(el('h3',ingredient.ingredient));
    for(const products of ingredient.groups){
      const representative=products.reduce((best,item)=>mfdsTier(item,terms||[])<mfdsTier(best,terms||[])?item:best,products[0]),historyProduct=representative.revision_count>1?representative:products.reduce((best,item)=>item.revision_count>best.revision_count?item:best,products[0]),group=el('details'),summary=el('summary');
      summary.textContent=`${products.length}개 품목 (${representative.item_name}${products.length>1?' 등':''})`;
      group.append(summary);
      const permit=representative.permit_date?dateLabel(representative.permit_date):'허가일 미상';
      const meta=el('p',`대표 품목: ${representative.item_name} · ${representative.entp_name} · ${permit}`,'meta');
      const source=el('a','식약처 원문');
      source.href=representative.source_url;source.target='_blank';source.rel='noopener';
      meta.append(document.createTextNode(' · '),source);
      group.append(meta);
      const indication=el('pre','펼쳐서 현재 효능·효과를 확인하세요.');
      group.append(indication);
      let loaded=false;
      group.addEventListener('toggle',()=>{if(!group.open||loaded)return;loaded=true;loadMfdsItem(representative,indication,true)});
      if(historyProduct.revision_count>1){
        const history=el('details'),historySummary=el('summary',`${historyProduct.item_name} 허가사항 변화 ${historyProduct.revision_count}건`),historyBody=el('pre','펼쳐서 변화 이력을 확인하세요.');
        history.append(historySummary,historyBody);
        let historyLoaded=false;
        history.addEventListener('toggle',()=>{if(!history.open||historyLoaded)return;historyLoaded=true;loadMfdsItem(historyProduct,historyBody,false)});
        group.append(history);
      }
      section.append(group);
    }
  }
  container.append(section);
}
function render(){const terms=q.value.trim().toLocaleLowerCase('ko').split(/\s+/).filter(Boolean);root.replaceChildren();if(!terms.length){status.textContent=`전체 ${rows.length.toLocaleString()}개 항목`;root.append(el('p','검색어를 입력해 주세요.','empty'));return}const hits=rows.filter(record=>{const hay=(record.title+'\n'+record.body+'\n'+record.class_header+'\n'+dateLabel(record.effective_date)+'\n'+record.effective_date+'\n고시 제'+record.notice_number+'호').toLocaleLowerCase('ko');return terms.some(term=>hay.includes(term))});const criteria=hits.filter(record=>record.class_no),documents=hits.filter(record=>!record.class_no),criterionGroups=groupCriteria(criteria),documentGroups=groupDocuments(documents),mfdsItems=(()=>{let pairs=(mfdsRows||[]).map(item=>[mfdsTier(item,terms),item]).filter(pair=>pair[0]<4);if(pairs.some(pair=>pair[0]===0))pairs=pairs.filter(pair=>pair[0]===0);return pairs.sort((a,b)=>a[0]-b[0]).map(pair=>pair[1])})();status.textContent=`급여기준 ${criterionGroups.length.toLocaleString()}개 · 개정 이력 ${criteria.length.toLocaleString()}건 · 관련 문서 ${documents.length.toLocaleString()}건${mfdsRows?` · 허가 품목 ${mfdsItems.length.toLocaleString()}개`:''}`;appendCriteria(criterionGroups,root);if(mfdsItems.length)appendMfds(mfdsItems,root,terms);if(documents.length){const related=el('details',undefined,'related');related.append(el('summary',`관련 고시문 및 첨부자료 ${documents.length.toLocaleString()}건`));appendDocuments(documentGroups,related);root.append(related)}}
const initialQuery=new URLSearchParams(location.search).get('q');if(initialQuery)q.value=initialQuery;
const syncUrl=()=>{const term=q.value.trim();history.replaceState(null,'',term?`?q=${encodeURIComponent(term)}`:location.pathname)};
fetch('search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(data=>{rows=data;render()}).catch(e=>{status.textContent='검색 자료를 불러오지 못했습니다.';console.error(e)});q.addEventListener('input',()=>{syncUrl();render()});
fetch('mfds/search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(data=>{mfdsRows=data;render()}).catch(()=>{mfdsRows=null});
</script></body></html>'''


def latest_notice_label() -> str:
    """수록된 가장 최근 발령 고시로 '최근 갱신' 문구를 만든다. 자료가 없으면 빈 문자열."""
    latest = ("", "")
    for path in sorted(NORMALIZED.glob("*.json")) if NORMALIZED.is_dir() else []:
        version = json.loads(path.read_text(encoding="utf-8")).get("version") or {}
        key = (str(version.get("발령일자") or ""), str(version.get("발령번호") or ""))
        if key[0] > latest[0]:
            latest = key
    if not latest[0]:
        return ""
    date = f"{latest[0][:4]}-{latest[0][4:6]}-{latest[0][6:8]}"
    return f"최근 갱신: {date} · "


SLUG_STRIP = re.compile("[^0-9a-z가-힣]+")


def slugify(title: str) -> str:
    """제목을 URL 슬러그로 만든다. 예) Dapagliflozin 경구제 → dapagliflozin-경구제"""
    slug = SLUG_STRIP.sub("-", title.casefold()).strip("-")
    return slug[:80].rstrip("-") or "criteria"


def criteria_groups() -> list[list[dict]]:
    """항목(block_identity)별 급여기준 개정 전문을 최신 순으로 묶는다."""
    groups: dict[str, list[dict]] = {}
    for path in sorted(NORMALIZED.glob("*.json")) if NORMALIZED.is_dir() else []:
        document = json.loads(path.read_text(encoding="utf-8"))
        version = document["version"]
        for entry in document["entries"]:
            if not entry["class_no"]:
                continue
            groups.setdefault(entry["block_identity"], []).append({
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


def build_criteria_pages() -> list[tuple[str, str]]:
    """항목별 정적 페이지를 쓰고 (제목, 상대 URL) 목록을 돌려준다."""
    output = PUBLIC / "criteria"
    if output.exists():
        shutil.rmtree(output)
    entries: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    label = latest_notice_label()
    for items in criteria_groups():
        newest = items[0]
        slug = slugify(newest["title"])
        used[slug] = used.get(slug, 0) + 1
        if used[slug] > 1:
            slug = f"{slug}-{used[slug]}"
        url_path = f"criteria/{quote(slug)}.html"
        page = criteria_page(newest, items, url_path).replace(FOOTER_LABEL_PLACEHOLDER, label)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{slug}.html").write_text(page + chr(10), encoding="utf-8")
        entries.append((newest["title"], url_path))
    return entries


def static_drug_list(catalog: list[tuple[str, str]]) -> str:
    """크롤러와 사용자가 항목 페이지로 진입하는 수록 약제 목록."""
    if not catalog:
        return ""
    items = "".join(
        f'<li><a href="{path}">{html_escape(title)}</a></li>' for title, path in catalog
    )
    return (f'<details class="catalog"><summary>수록된 약제 급여기준 {len(catalog):,}건 목록</summary>'
            f"<ul>{items}</ul></details>")


def write_crawler_files(page_urls: list[str]) -> None:
    """sitemap.xml과 robots.txt를 생성한다.

    프로젝트 페이지라 robots.txt는 루트가 아니어서 크롤러 효력은 없지만,
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


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    index = build_index()
    mfds_index = build_mfds_public()
    (PUBLIC / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
    page = HTML.replace("__LATEST_NOTICE__", latest_notice_label())
    catalog = build_criteria_pages()
    page = page.replace("__DRUG_CATALOG__", static_drug_list(catalog))
    write_crawler_files([SITE_URL] + [SITE_URL + path for _, path in catalog])
    (PUBLIC / "index.html").write_text(page + "\n", encoding="utf-8")
    print(f"검색 항목 {len(index)}개, 허가 품목 {len(mfds_index)}개, 기준 페이지 {len(catalog)}개를 {PUBLIC}에 생성했습니다.")


if __name__ == "__main__":
    main()
