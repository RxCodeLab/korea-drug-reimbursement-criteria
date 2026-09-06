from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from html import escape as html_escape

from common import BASE, DATA
from search import ACTION_LABELS, ROLE_ORDER

NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
PUBLIC = BASE / "public"
SITE_URL = "https://rxcodelab.github.io/korea-drug-reimbursement-criteria/"
REPO_URL = "https://github.com/RxCodeLab/korea-drug-reimbursement-criteria"
FOOTER_LABEL_PLACEHOLDER = "__FOOTER_LABEL__"
MAX_BODY_CHARS = 50_000
# 허가 색인 행의 열 순서. 브라우저는 이 순서로 객체를 복원한다. 24,596행이라 키 이름을 행마다 싣지 않는다.
MFDS_INDEX_FIELDS = (
    "item_seq", "item_name", "entp_name", "main_item_ingr", "main_item_ingr_eng",
    "permit_date", "revision_count", "content_key",
)
MFDS_CONTENT_KEY_CHARS = 12
KST = timezone(timedelta(hours=9))


def load_normalized() -> list[dict]:
    """정규화 문서를 한 번만 읽는다. 색인·목록·상세 페이지·최근 갱신 문구가 모두 이 목록을 쓴다."""
    if not NORMALIZED.is_dir():
        return []
    documents = []
    for path in sorted(NORMALIZED.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("complete") is not True:
            raise RuntimeError(f"내용이 온전하지 않은 정규화 문서입니다: {path}")
        documents.append(document)
    return documents


def build_index(documents: list[dict]) -> list[dict]:
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
    return sorted(records, key=lambda r: (r["key"], r["effective_date"], r["sequence"], r["source_sha256"]))


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def build_mfds_public() -> list[list[object]]:
    """허가 품목 색인(열 배열)과 품목별 상세 JSON을 쓴다."""
    rows: list[list[object]] = []
    output = PUBLIC / "mfds"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if not MFDS_ITEMS.is_dir():
        (output / "search-index.json").write_text(_compact_json({"fields": list(MFDS_INDEX_FIELDS), "rows": rows}), encoding="utf-8")
        return rows
    items_dir = output / "items"
    for path in sorted(MFDS_ITEMS.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("complete") is not True:
            raise RuntimeError(f"내용이 온전하지 않은 MFDS 항목입니다: {path}")
        revisions = record.get("revisions") or []
        rows.append([
            record["item_seq"],
            record["item_name"],
            record["entp_name"],
            record["main_item_ingr"],
            record.get("main_item_ingr_eng", ""),
            record["permit_date"],
            len(revisions),
            revisions[0]["content_sha256"][:MFDS_CONTENT_KEY_CHARS] if revisions else "",
        ])
        target = items_dir / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_compact_json(record), encoding="utf-8")
    rows.sort(key=lambda r: (r[1], r[0]))
    (output / "search-index.json").write_text(_compact_json({"fields": list(MFDS_INDEX_FIELDS), "rows": rows}), encoding="utf-8")
    return rows


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
body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1d2433}input{width:100%;box-sizing:border-box;padding:.8rem;font-size:1rem;border:1px solid #8993a4;border-radius:6px}.hint,.meta{color:#5b6575}.group{margin:1.5rem 0;border-top:2px solid #28364d}.group h2{font-size:1.15rem}details{border:1px solid #ccd2dc;border-radius:6px;margin:.5rem 0;padding:.5rem .8rem}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:.8rem}.badge{color:#a33;margin-left:.5rem}.revision{font-weight:700;margin:.6rem 0 .2rem}.empty{padding:2rem 0;color:#5b6575}.related{margin-top:2rem;border-color:#8993a4}.related>.group{margin-left:.5rem}.catalog{margin-top:2rem;color:#5b6575;font-size:.9rem}.catalog ul{columns:2;margin:.5rem 0;padding-left:1.2rem}.warn{color:#a33}footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid #ccd2dc;color:#5b6575;font-size:.9rem}
</style></head><body>
<h1>약제 급여기준 변경 이력 검색</h1><p class="hint">한글 또는 영문 검색어를 공백으로 나누어 입력하면, 하나라도 포함된 항목을 표시합니다.</p>
<input id="q" type="search" autocomplete="off" placeholder="예: dapagliflozin 다파글리플로진" autofocus><p id="status" class="meta"></p><main id="results"></main>
__DRUG_CATALOG__
<footer>__FOOTER_LABEL__<a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">데이터 수집·검증 과정 보기</a></footer>
<script>
const q=document.querySelector('#q'),status=document.querySelector('#status'),root=document.querySelector('#results');
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const actionLabels=__ACTION_LABELS__;
const actionLabel=action=>actionLabels[action]??action;
const roleRanks=__ROLE_RANKS__;
const roleRank=role=>roleRanks[role]??Object.keys(roleRanks).length;
const MFDS_DETAIL_URL='https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=';
const dateLabel=date=>`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6)}`;
const versionHeader=record=>`${dateLabel(record.effective_date)} 시행 · 고시 제${record.notice_number}호`;
const noticeLink=seq=>{const a=el('a','국가법령정보센터 원문');a.href=`https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq=${encodeURIComponent(seq)}`;a.target='_blank';a.rel='noopener';return a};
const byNewest=(a,b)=>b.effective_date.localeCompare(a.effective_date)||b.sequence.localeCompare(a.sequence);
const byRole=(a,b)=>roleRank(a.role)-roleRank(b.role)||a.ordinal-b.ordinal;
const baseName=name=>name.replace(/[(].*$/,'');
const brandName=name=>baseName(name).replace(/[0-9][0-9./]*(밀리그램|밀리그람|그램|그람|밀리리터|리터|마이크로그램|mg|㎎|ml|㎖|g|iu|%|만단위|단위).*$/i,'').trim().toLocaleLowerCase('ko');
// 검색 키는 로드 시 한 번만 계산한다. 키 입력마다 2만 행의 문자열을 다시 만들면 입력이 끊긴다.
const searchableCriterion=record=>({...record,hay:(record.title+'\n'+record.body+'\n'+record.class_header+'\n'+dateLabel(record.effective_date)+'\n'+record.effective_date+'\n고시 제'+record.notice_number+'호').toLocaleLowerCase('ko')});
const searchableMfds=item=>({...item,source_url:MFDS_DETAIL_URL+encodeURIComponent(item.item_seq),brand:brandName(item.item_name),nameKey:item.item_name.toLocaleLowerCase('ko'),exactKey:(item.entp_name+'|'+item.main_item_ingr+'|'+item.main_item_ingr_eng).toLocaleLowerCase('ko')});
const mfdsTier=(item,terms)=>{if(terms.some(t=>item.brand===t))return 0;if(terms.some(t=>item.brand.startsWith(t)))return 1;if(terms.some(t=>item.nameKey.includes(t)))return 2;if(terms.some(t=>item.exactKey.includes(t)))return 3;return 4};
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
    groups:groupsBy(products,'content_key').map(group=>group.sort((a,b)=>{
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
// 데이터셋별 로딩 상태. 실패는 조용히 넘기지 않고 status 문구에 남긴다.
const data={criteria:{rows:null,state:'loading'},mfds:{rows:null,state:'idle'}};
function stateNote(){const notes=[];if(data.criteria.state==='failed')notes.push('급여기준 색인을 불러오지 못했습니다');if(data.mfds.state==='failed')notes.push('허가 품목 색인을 불러오지 못했습니다');if(data.mfds.state==='loading')notes.push('허가 품목 불러오는 중…');return notes.length?` · ${notes.join(' · ')}`:''}
function render(){const terms=q.value.trim().toLocaleLowerCase('ko').split(/\s+/).filter(Boolean);root.replaceChildren();const rows=data.criteria.rows||[];if(!terms.length){status.textContent=`전체 ${rows.length.toLocaleString()}개 항목${stateNote()}`;root.append(el('p','검색어를 입력해 주세요.','empty'));return}ensureMfds();const hits=rows.filter(record=>terms.some(term=>record.hay.includes(term)));const criteria=hits.filter(record=>record.class_no),documents=hits.filter(record=>!record.class_no),criterionGroups=groupCriteria(criteria),documentGroups=groupDocuments(documents);
// 정확 일치는 순위만 올린다. 정확 일치가 있다고 다른 검색어의 결과를 지우면 안내한 OR 검색과 어긋난다.
const mfdsItems=(data.mfds.rows||[]).map(item=>[mfdsTier(item,terms),item]).filter(pair=>pair[0]<4).sort((a,b)=>a[0]-b[0]).map(pair=>pair[1]);status.textContent=`급여기준 ${criterionGroups.length.toLocaleString()}개 · 개정 이력 ${criteria.length.toLocaleString()}건 · 관련 문서 ${documents.length.toLocaleString()}건${data.mfds.rows?` · 허가 품목 ${mfdsItems.length.toLocaleString()}개`:''}${stateNote()}`;appendCriteria(criterionGroups,root);if(mfdsItems.length)appendMfds(mfdsItems,root,terms);if(documents.length){const related=el('details',undefined,'related');related.append(el('summary',`관련 고시문 및 첨부자료 ${documents.length.toLocaleString()}건`));appendDocuments(documentGroups,related);root.append(related)}}
const initialQuery=new URLSearchParams(location.search).get('q');if(initialQuery)q.value=initialQuery;
const syncUrl=()=>{const term=q.value.trim();history.replaceState(null,'',term?`?q=${encodeURIComponent(term)}`:location.pathname)};
let renderTimer=null;const scheduleRender=()=>{clearTimeout(renderTimer);renderTimer=setTimeout(render,150)};
fetch('search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(rows=>{data.criteria={rows:rows.map(searchableCriterion),state:'ready'};render()}).catch(e=>{data.criteria={rows:[],state:'failed'};render();console.error(e)});
// 허가 색인은 급여 색인보다 크므로 첫 검색어가 들어올 때 한 번만 받는다.
function ensureMfds(){if(data.mfds.state!=='idle')return;data.mfds.state='loading';fetch('mfds/search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(index=>{const fields=index.fields;data.mfds={rows:index.rows.map(row=>searchableMfds(Object.fromEntries(fields.map((field,i)=>[field,row[i]])))),state:'ready'};render()}).catch(e=>{data.mfds={rows:null,state:'failed'};render();console.error(e)})}
q.addEventListener('input',()=>{syncUrl();scheduleRender()});
</script></body></html>'''


def latest_notice_label(documents: list[dict]) -> str:
    """수록된 가장 최근 발령 고시로 '최근 갱신' 문구를 만든다. 자료가 없으면 빈 문자열."""
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


def collection_status_label(raw: str | None) -> str:
    """워크플로가 COLLECTION_STATUS로 넘긴 마지막 수집 시도 결과. 수집 실패를 발령일자 뒤에 숨기지 않는다."""
    if not raw:
        return ""
    try:
        status = json.loads(raw)
        run_at = datetime.fromisoformat(str(status["run_at"]).replace("Z", "+00:00")).astimezone(KST)
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"COLLECTION_STATUS 형식이 잘못되었습니다: {raw!r}") from None
    labels = {"success": "성공", "failure": "실패", "skipped": "건너뜀", "cancelled": "취소"}
    parts = [f"마지막 수집 시도: {run_at.strftime('%Y-%m-%d %H:%M')} KST"]
    for key, name in (("law", "법제처"), ("mfds", "식약처")):
        outcome = str(status.get(key) or "")
        label = labels.get(outcome, outcome or "?")
        parts.append(f"{name} {label}" if outcome == "success" else f'<span class="warn">{name} {html_escape(label)}</span>')
    return " · ".join(parts) + " · "


SLUG_STRIP = re.compile("[^0-9a-z가-힣]+")


def slugify(title: str) -> str:
    """제목을 URL 슬러그로 만든다. 예) Dapagliflozin 경구제 → dapagliflozin-경구제"""
    slug = SLUG_STRIP.sub("-", title.casefold()).strip("-")
    return slug[:80].rstrip("-") or "criteria"


def identity_slug(block_identity: str) -> str:
    """항목 식별자(block_identity)로 만든 슬러그. 최신 품명이 바뀌어도 URL이 유지된다.

    NFKC로 Ⅷ→VIII 같은 호환 문자를 풀어 혐액응고인자 Ⅷ/Ⅸ처럼 로마숫자만 다른 식별자가 같은 슬러그로 무너지지 않게 한다.
    """
    return slugify(unicodedata.normalize("NFKC", block_identity).replace("[", "").replace("]", "-"))


def page_slugs(identities: list[str]) -> dict[str, str]:
    """식별자별 페이지 슬러그. 슬러그가 겹치는 식별자는 양쪽 모두 식별자 해시를 붙여, 정렬 순서와 무관하게 URL이 고정된다."""
    base = {identity: identity_slug(identity) for identity in identities}
    counts: dict[str, int] = {}
    for slug in base.values():
        counts[slug] = counts.get(slug, 0) + 1
    return {
        identity: slug if counts[slug] == 1 else f"{slug}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:6]}"
        for identity, slug in base.items()
    }


def criteria_groups(documents: list[dict]) -> list[list[dict]]:
    """항목(block_identity)별 급여기준 개정 전문을 최신 순으로 묶는다."""
    groups: dict[str, list[dict]] = {}
    for document in documents:
        version = document["version"]
        for entry in document["entries"]:
            if not entry["class_no"]:
                continue
            groups.setdefault(entry["block_identity"], []).append({
                "identity": entry["block_identity"],
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
        '.warn{color:#a33}footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid #ccd2dc;'
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


def build_criteria_pages(documents: list[dict], footer_label: str) -> list[tuple[str, str]]:
    """항목별 정적 페이지를 쓰고 (제목, 상대 URL) 목록을 돌려준다."""
    output = PUBLIC / "criteria"
    if output.exists():
        shutil.rmtree(output)
    entries: list[tuple[str, str]] = []
    groups = criteria_groups(documents)
    slugs = page_slugs([items[0]["identity"] for items in groups])
    for items in groups:
        newest = items[0]
        slug = slugs[newest["identity"]]
        url_path = f"criteria/{quote(slug)}.html"
        page = criteria_page(newest, items, url_path).replace(FOOTER_LABEL_PLACEHOLDER, footer_label)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{slug}.html").write_text(page + chr(10), encoding="utf-8")
        entries.append((newest["title"], url_path))
    return entries


def static_drug_list(catalog: list[tuple[str, str]]) -> str:
    """수록 약제 목록. 크롤러와 사용자가 여기서 항목 페이지로 들어간다."""
    if not catalog:
        return ""
    items = "".join(
        f'<li><a href="{path}">{html_escape(title)}</a></li>' for title, path in catalog
    )
    return (f'<details class="catalog"><summary>수록된 약제 급여기준 {len(catalog):,}건 목록</summary>'
            f"<ul>{items}</ul></details>")


def write_crawler_files(page_urls: list[str]) -> None:
    """sitemap.xml과 robots.txt를 생성한다.

    프로젝트 페이지여서 robots.txt가 루트에 놓이지 않아 크롤러에는 효력이 없지만,
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


def render_index_page(catalog: list[tuple[str, str]], footer_label: str) -> str:
    role_ranks = {role: rank for rank, role in enumerate(ROLE_ORDER)}
    return (
        HTML.replace("__ACTION_LABELS__", json.dumps(ACTION_LABELS, ensure_ascii=False, separators=(",", ":")))
        .replace("__ROLE_RANKS__", json.dumps(role_ranks, ensure_ascii=False, separators=(",", ":")))
        .replace(FOOTER_LABEL_PLACEHOLDER, footer_label)
        .replace("__DRUG_CATALOG__", static_drug_list(catalog))
    )


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    documents = load_normalized()
    index = build_index(documents)
    mfds_rows = build_mfds_public()
    (PUBLIC / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
    footer_label = latest_notice_label(documents) + collection_status_label(os.environ.get("COLLECTION_STATUS"))
    catalog = build_criteria_pages(documents, footer_label)
    write_crawler_files([SITE_URL] + [SITE_URL + path for _, path in catalog])
    (PUBLIC / "index.html").write_text(render_index_page(catalog, footer_label) + "\n", encoding="utf-8")
    print(f"검색 항목 {len(index)}개, 허가 품목 {len(mfds_rows)}개, 기준 페이지 {len(catalog)}개를 {PUBLIC}에 생성했습니다.")


if __name__ == "__main__":
    main()
