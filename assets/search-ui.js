// DOM 조립과 빌드 타임에 주입되는 라벨·순서 데이터. 순수 로직은 search-core.js에 있다.
// 주석에도 플레이스홀더 이름을 그대로 쓰지 말 것 — render_index_page가 문자열 치환이라 함께 바뀐다.
const {MFDS_GROUP_LIMIT,TRUNCATED,dateLabel,searchableCriterion,distinctClassHeader,byNewest,groupsBy,groupCriteria,buildMfdsIndex,matchMfds,materializeMfds,indicationGroups}=SearchCore;
const q=document.querySelector('#q'),status=document.querySelector('#status'),root=document.querySelector('#results');
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const actionLabels=__ACTION_LABELS__;
const actionLabel=action=>actionLabels[action]??action;
const roleRanks=__ROLE_RANKS__;
const roleRank=role=>roleRanks[role]??Object.keys(roleRanks).length;
const versionHeader=record=>`${dateLabel(record.effective_date)} 시행 · 고시 제${record.notice_number}호`;
const noticeLink=seq=>{const a=el('a','국가법령정보센터 원문');a.href=`https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq=${encodeURIComponent(seq)}`;a.target='_blank';a.rel='noopener';return a};
const byRole=(a,b)=>roleRank(a.role)-roleRank(b.role)||a.ordinal-b.ordinal;
function groupDocuments(records){return groupsBy(records,'sequence').map(items=>items.sort(byRole)).sort((a,b)=>byNewest(a[0],b[0]))}
function appendCriteria(groups,container){for(const items of groups){const section=el('section',undefined,'group'),newest=items[0];section.append(el('h2',newest.title));if(distinctClassHeader(newest))section.append(el('p',newest.class_header,'meta'));for(const record of items){const details=el('details'),summary=el('summary');summary.append(document.createTextNode(versionHeader(record)));summary.append(el('span',actionLabel(record.action),'badge'));details.append(summary);const source=el('p',`출처: ${record.source_name} · `,'meta');source.append(noticeLink(record.sequence));details.append(source);details.append(el('pre',record.body+(record.truncated?TRUNCATED:'')));section.append(details)}container.append(section)}}
function appendDocuments(groups,container){for(const items of groups){const section=el('section',undefined,'group'),head=el('p',undefined,'revision');head.append(document.createTextNode(versionHeader(items[0])));for(const role of[...new Set(items.map(item=>item.role))].sort((a,b)=>roleRank(a)-roleRank(b)))head.append(el('span',actionLabel(role),'badge'));head.append(document.createTextNode(' · '));head.append(noticeLink(items[0].sequence));section.append(head);for(const record of items){const details=el('details'),summary=el('summary');summary.append(el('span',actionLabel(record.role),'badge'));summary.append(document.createTextNode(` ${record.source_name}`));details.append(summary);details.append(el('pre',record.body+(record.truncated?TRUNCATED:'')));section.append(details)}container.append(section)}}
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
function mfdsGroupNode(index,bucket){
  const products=bucket.map(match=>materializeMfds(index,match[1],match[0]));
  const representative=products.reduce((best,item)=>item.tier<best.tier?item:best,products[0]),historyProduct=representative.revision_count>1?representative:products.reduce((best,item)=>item.revision_count>best.revision_count?item:best,products[0]),group=el('details'),summary=el('summary');
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
  return group;
}
// '정'·'산'처럼 흔한 한 글자는 품목 그룹이 수천 개 걸린다(실측: '산' 7,953개, DOM 43,312노드).
// 전량 수집으로 게시 품목이 43,017건이 되면서 한 번에 다 그리면 메인 스레드가 1초 가까이 멈춘다.
// 관련도 순으로 MFDS_GROUP_LIMIT개씩 끊어 그리고 나머지는 버튼으로 이어 그린다. 총 건수는
// 상태 문구('허가 품목 N개')가 계속 실제 값을 보여주므로 결과를 감추는 것은 아니다.
function appendMfds(index,matches,container){
  const section=el('section',undefined,'group mfds');
  section.append(el('h2','식약처 허가 적응증'));
  section.append(el('p','효능·효과가 같은 품목은 함께 표시합니다. 식약처 허가 품목 전체를 대상으로 하며, 건강보험 급여 여부와 무관합니다.','meta'));
  const units=[];
  for(const ingredient of indicationGroups(index,matches))for(const bucket of ingredient.groups)units.push([ingredient.ingredient,bucket]);
  const more=el('button',undefined,'more');more.type='button';
  let drawn=0,heading=null;
  const draw=()=>{
    const end=Math.min(drawn+MFDS_GROUP_LIMIT,units.length),frag=document.createDocumentFragment();
    for(;drawn<end;drawn++){const[ingredient,bucket]=units[drawn];if(ingredient!==heading){heading=ingredient;frag.append(el('h3',ingredient))}frag.append(mfdsGroupNode(index,bucket))}
    section.insertBefore(frag,more);
    const left=units.length-drawn;
    more.hidden=!left;
    if(left)more.textContent=`남은 품목 그룹 ${left.toLocaleString()}개 더 보기`;
  };
  more.addEventListener('click',draw);
  section.append(more);
  draw();
  container.append(section);
}
// 데이터셋별 로딩 상태. 실패는 조용히 넘기지 않고 status 문구에 남긴다.
const data={criteria:{rows:null,state:'loading'},mfds:{index:null,state:'idle'}};
function stateNote(){const notes=[];if(data.criteria.state==='failed')notes.push('급여기준 색인을 불러오지 못했습니다');if(data.mfds.state==='failed')notes.push('허가 품목 색인을 불러오지 못했습니다');if(data.mfds.state==='loading')notes.push('허가 품목 불러오는 중…');return notes.length?` · ${notes.join(' · ')}`:''}
function render(){const terms=q.value.trim().toLocaleLowerCase('ko').split(/\s+/).filter(Boolean);root.replaceChildren();const rows=data.criteria.rows||[];if(!terms.length){status.textContent=`전체 ${rows.length.toLocaleString()}개 항목${stateNote()}`;root.append(el('p','검색어를 입력해 주세요.','empty'));return}ensureMfds();const hits=rows.filter(record=>terms.some(term=>record.hay.includes(term)));const criteria=hits.filter(record=>record.class_no),documents=hits.filter(record=>!record.class_no),criterionGroups=groupCriteria(criteria),documentGroups=groupDocuments(documents);
// 정확 일치는 순위만 올린다. 정확 일치가 있다고 다른 검색어의 결과를 지우면 안내한 OR 검색과 어긋난다.
const mfdsMatches=data.mfds.index?matchMfds(data.mfds.index,terms):[];status.textContent=`급여기준 ${criterionGroups.length.toLocaleString()}개 · 개정 이력 ${criteria.length.toLocaleString()}건 · 관련 문서 ${documents.length.toLocaleString()}건${data.mfds.index?` · 허가 품목 ${mfdsMatches.length.toLocaleString()}개`:''}${stateNote()}`;appendCriteria(criterionGroups,root);if(mfdsMatches.length)appendMfds(data.mfds.index,mfdsMatches,root);if(documents.length){const related=el('details',undefined,'related');related.append(el('summary',`관련 고시문 및 첨부자료 ${documents.length.toLocaleString()}건`));appendDocuments(documentGroups,related);root.append(related)}}
const initialQuery=new URLSearchParams(location.search).get('q');if(initialQuery)q.value=initialQuery;
const syncUrl=()=>{const term=q.value.trim();history.replaceState(null,'',term?`?q=${encodeURIComponent(term)}`:location.pathname)};
let renderTimer=null;const scheduleRender=()=>{clearTimeout(renderTimer);renderTimer=setTimeout(render,150)};
fetch('search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(rows=>{data.criteria={rows:rows.map(searchableCriterion),state:'ready'};render()}).catch(e=>{data.criteria={rows:[],state:'failed'};render();console.error(e)});
// 허가 색인은 급여 색인보다 크므로 첫 검색어가 들어올 때 한 번만 받는다.
function ensureMfds(){if(data.mfds.state!=='idle')return;data.mfds.state='loading';fetch('mfds/search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(index=>{data.mfds={index:buildMfdsIndex(index),state:'ready'};render()}).catch(e=>{data.mfds={index:null,state:'failed'};render();console.error(e)})}
q.addEventListener('input',()=>{syncUrl();scheduleRender()});
