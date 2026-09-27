// DOM rendering. render_index_page substitutes placeholders by plain string replace, so never spell them out in comments.
const {MFDS_GROUP_LIMIT,CRITERIA_LIMIT,TRUNCATED,dateLabel,searchableCriterion,distinctClassHeader,byNewest,groupsBy,groupCriteria,buildMfdsIndex,matchMfds,materializeMfds,indicationGroups,productTerms,productResults,searchTerms}=SearchCore;
const q=document.querySelector('#q'),status=document.querySelector('#status'),root=document.querySelector('#results'),intro=document.querySelector('#intro');
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const actionLabels=__ACTION_LABELS__;
const actionLabel=action=>actionLabels[action]??action;
const roleRanks=__ROLE_RANKS__;
const roleRank=role=>roleRanks[role]??Object.keys(roleRanks).length;
// Keeps the date and notice number from breaking at their hyphens on narrow screens.
const versionHeader=record=>{const f=document.createDocumentFragment();f.append(el('span',`${dateLabel(record.effective_date)} 시행`,'nw'),' · ',el('span',`고시 제${record.notice_number}호`,'nw'));return f};
const noticeLink=seq=>{const a=el('a','국가법령정보센터 원문');a.href=`https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq=${encodeURIComponent(seq)}`;a.target='_blank';a.rel='noopener';return a};
// data-kind selects the badge color in CSS.
const badge=(text,kind)=>{const n=el('span',text,'badge');if(kind)n.dataset.kind=kind;return n};
const byRole=(a,b)=>roleRank(a.role)-roleRank(b.role)||a.ordinal-b.ordinal;
function groupDocuments(records){return groupsBy(records,'sequence').map(items=>items.sort(byRole)).sort((a,b)=>byNewest(a[0],b[0]))}
// Builds the content on first open; bodies can be tens of KB each.
function lazyDetails(summary,build,cls){
  const details=el('details',undefined,cls);details.append(summary);
  let built=false;
  details.addEventListener('toggle',()=>{if(!details.open||built)return;built=true;build(details)});
  return details;
}
const bodyText=record=>record.body+(record.truncated?TRUNCATED:'');
function revisionNode(record,latest){
  const summary=el('summary');
  if(latest)summary.append(badge('최신','latest'),' ');
  summary.append(versionHeader(record),' ',badge(actionLabel(record.action),record.action));
  return lazyDetails(summary,details=>{
    const source=el('p',`출처: ${record.source_name} · `,'meta');source.append(noticeLink(record.sequence));
    details.append(source,el('pre',bodyText(record)));
  },latest?'rev latest':'rev');
}
// Draws count items limit at a time, with a '더 보기' button for the rest.
function paged(section,count,limit,drawOne,moreLabel){
  const more=el('button',undefined,'more');more.type='button';
  let drawn=0;
  const draw=()=>{
    const end=Math.min(drawn+limit,count),frag=document.createDocumentFragment();
    for(;drawn<end;drawn++)drawOne(drawn,frag);
    section.insertBefore(frag,more);
    const left=count-drawn;
    more.hidden=!left;
    if(left)more.textContent=moreLabel(left.toLocaleString());
  };
  more.addEventListener('click',draw);
  section.append(more);
  draw();
}
// items holds only matching revisions, so the first one is labeled latest only if it really is.
function criterionNode(items){
  const article=el('article',undefined,'criterion'),newest=items[0],latest=data.criteria.latest.get(newest.key);
  article.append(el('h3',newest.title));
  const meta=[];
  if(distinctClassHeader(newest))meta.push(newest.class_header);
  if(latest)meta.push(`최근 개정 ${dateLabel(latest.effective_date)} 시행`);
  meta.push(`개정 이력 ${(data.criteria.revisionsOf.get(newest.key)||items.length).toLocaleString()}건`);
  article.append(el('p',meta.join(' · '),'meta'));
  article.append(revisionNode(newest,latest===newest));
  if(items.length>1){
    const older=items.slice(1);
    article.append(lazyDetails(el('summary',`이전 개정 ${older.length.toLocaleString()}건`),details=>{for(const record of older)details.append(revisionNode(record,false))},'older'));
  }
  return article;
}
function criteriaNode(groups){
  const section=el('section',undefined,'block');section.id='sec-criteria';
  section.append(el('h2',`급여기준 ${groups.length.toLocaleString()}개`));
  paged(section,groups.length,CRITERIA_LIMIT,(i,frag)=>frag.append(criterionNode(groups[i])),left=>`남은 급여기준 ${left}개 더 보기`);
  return section;
}
function appendDocuments(groups,container){
  for(const items of groups){
    const section=el('section',undefined,'notice'),head=el('p',undefined,'revision');
    head.append(versionHeader(items[0]));
    for(const role of[...new Set(items.map(item=>item.role))].sort((a,b)=>roleRank(a)-roleRank(b)))head.append(' ',badge(actionLabel(role),'role'));
    head.append(' · ',noticeLink(items[0].sequence));
    section.append(head);
    for(const record of items){
      const summary=el('summary');summary.append(badge(actionLabel(record.role),'role'),` ${record.title}`);
      section.append(lazyDetails(summary,details=>details.append(el('pre',bodyText(record))),'rev'));
    }
    container.append(section);
  }
}
// Resolves false on failure so the caller can allow another try.
function loadMfdsItem(item,target,currentOnly){
  target.textContent='불러오는 중…';
  return fetch(`mfds/items/${item.item_seq}.json`).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(doc=>{
    const revisions=doc.revisions||[];
    if(!revisions.length){target.textContent='효능·효과 정보가 없습니다.';return true;}
    if(currentOnly){target.textContent=revisions[0].ee_text;return true;}
    target.textContent=revisions.map((revision,index)=>{
      const label=index===0?'현재':'이전';
      const date=revision.official_revision_date?`허가사항 변경일 ${revision.official_revision_date}`:`최초 관찰 ${revision.first_observed_at.slice(0,10)} · 최종 관찰 ${revision.last_observed_at.slice(0,10)}`;
      return `[${label} · ${date}]\n${revision.ee_text}`;
    }).join('\n\n');
    return true;
  }).catch(()=>{target.textContent='상세 정보를 불러오지 못했습니다. 접었다가 다시 펼쳐 보세요.';return false});
}
// Only the representative (best tier) and the product with the longest history become objects; a group
// of identical injections can hold thousands of products.
function mfdsGroupNode(index,bucket){
  let best=bucket[0],richest=bucket[0];
  for(const match of bucket){
    if(match[0]<best[0])best=match;
    if(index.revisions[match[1]]>index.revisions[richest[1]])richest=match;
  }
  const representative=materializeMfds(index,best[1],best[0]);
  const historyProduct=representative.revision_count>1?representative:materializeMfds(index,richest[1],richest[0]);
  const group=el('details'),summary=el('summary');
  summary.append(representative.item_name);
  if(representative.withdrawn)summary.append(el('span',` (${representative.withdrawn})`,'count'));
  if(bucket.length>1)summary.append(el('span',` 외 ${(bucket.length-1).toLocaleString()}개`,'count'));
  group.append(summary);
  const permit=representative.permit_date?dateLabel(representative.permit_date):'허가일 미상';
  const meta=el('p',[`대표 품목: ${representative.item_name}`,representative.entp_name,permit,representative.withdrawn].filter(Boolean).join(' · '),'meta');
  const source=el('a','식약처 원문');
  source.href=representative.source_url;source.target='_blank';source.rel='noopener';
  meta.append(document.createTextNode(' · '),source);
  group.append(meta);
  const indication=el('pre','펼쳐서 현재 효능·효과를 확인하세요.');
  group.append(indication);
  let loaded=false;
  group.addEventListener('toggle',()=>{if(!group.open||loaded)return;loaded=true;loadMfdsItem(representative,indication,true).then(ok=>{loaded=ok})});
  if(historyProduct.revision_count>1){
    const history=el('details'),historySummary=el('summary',`${historyProduct.item_name} 허가사항 변화 ${historyProduct.revision_count}건`),historyBody=el('pre','펼쳐서 변화 이력을 확인하세요.');
    history.append(historySummary,historyBody);
    let historyLoaded=false;
    history.addEventListener('toggle',()=>{if(!history.open||historyLoaded)return;historyLoaded=true;loadMfdsItem(historyProduct,historyBody,false).then(ok=>{historyLoaded=ok})});
    group.append(history);
  }
  return group;
}
function mfdsNode(index,matches){
  const section=el('section',undefined,'block mfds');section.id='sec-mfds';
  section.append(el('h2',`식약처 허가 품목 ${matches.length.toLocaleString()}개`));
  section.append(el('p','효능·효과가 같은 품목은 함께 묶었습니다. 건강보험 급여 여부와 무관한 식약처 허가 정보입니다.','meta'));
  const units=[];
  for(const ingredient of indicationGroups(index,matches))for(const bucket of ingredient.groups)units.push([ingredient.ingredient,bucket]);
  let heading=null;
  paged(section,units.length,MFDS_GROUP_LIMIT,(i,frag)=>{
    const[ingredient,bucket]=units[i];
    if(ingredient!==heading){heading=ingredient;frag.append(el('h3',ingredient))}
    frag.append(mfdsGroupNode(index,bucket));
  },left=>`남은 품목 그룹 ${left}개 더 보기`);
  return section;
}
// Scrolls without touching location.hash so the ?q= URL stays intact.
function jump(label,id){
  const a=el('a',label);a.href=`#${id}`;
  a.addEventListener('click',event=>{event.preventDefault();const target=document.getElementById(id);if(!target)return;if(target.tagName==='DETAILS')target.open=true;target.scrollIntoView({block:'start'})});
  return a;
}
const setStatus=parts=>{status.replaceChildren();parts.filter(Boolean).forEach((part,i)=>{if(i)status.append(' · ');status.append(part)})};
// mfds tracks only the full product index, which is downloaded when a query needs too many blocks.
const data={criteria:{rows:null,latest:new Map(),revisionsOf:new Map(),state:'loading'},mfds:{full:null,promise:null,state:'idle'},documents:{rows:null,state:'idle'}};
const PRODUCT_NOTES={short:'허가 품목은 두 글자 이상 입력하면 찾습니다',loading:'허가 품목 불러오는 중…',failed:'허가 품목을 불러오지 못했습니다',stale:'사이트가 갱신되었습니다. 새로고침해 주세요.'};
function stateNotes(){
  const notes=[];
  if(data.criteria.state==='loading')notes.push('급여기준 색인 불러오는 중…');
  if(data.criteria.state==='failed')notes.push('급여기준 색인을 불러오지 못했습니다');
  if(view&&PRODUCT_NOTES[view.products.state])notes.push(PRODUCT_NOTES[view.products.state]);
  if(view&&data.documents.state==='loading')notes.push('관련 문서 불러오는 중…');
  if(data.documents.state==='failed')notes.push('관련 문서 색인을 불러오지 못했습니다');
  return notes;
}
const queryTerms=()=>searchTerms(q.value);
const matching=(records,terms)=>(records||[]).filter(record=>terms.some(term=>record.hay.includes(term)));
// Results for the query on screen. Datasets that arrive later replace only their own section, so open
// entries and '더 보기' pages elsewhere survive.
let view=null;
function documentsNode(documents){
  if(!documents.length)return null;
  const heading=el('summary'),related=lazyDetails(heading,details=>appendDocuments(groupDocuments(documents),details),'block related');
  heading.append(el('h2',`관련 고시문 및 첨부자료 ${documents.length.toLocaleString()}건`));
  related.id='sec-docs';
  return related;
}
function renderStatus(){
  const count=(n,label,unit,id)=>{const text=`${label} ${n.toLocaleString()}${unit}`;return n?jump(text,id):text};
  setStatus(data.criteria.state==='loading'?stateNotes():[
    count(view.criteria.length,'급여기준','개','sec-criteria'),
    view.products.state==='ready'&&count(view.products.matches.length,'허가 품목','개','sec-mfds'),
    data.documents.rows&&count(view.documents.length,'관련 문서','건','sec-docs'),
    ...stateNotes(),
  ]);
  root.querySelector(':scope>.empty')?.remove();
  const settled=data.criteria.state==='ready'&&view.products.state!=='loading'&&(data.documents.rows||data.documents.state==='failed');
  if(!root.childElementCount&&settled)root.append(el('p','검색 결과가 없습니다. 성분명이나 제품명으로 다시 찾아보세요.','empty'));
}
// force: redraw even when the terms are unchanged (the criteria index just arrived).
function render(force){
  const terms=queryTerms();
  // A failed product search is retried when the same query is entered again.
  if(!force&&view&&view.query===terms.join(' ')&&view.products.state!=='failed')return;
  root.replaceChildren();intro.hidden=terms.length>0;view=null;
  if(!terms.length){setStatus(data.criteria.state==='ready'?[`급여기준 ${data.criteria.latest.size.toLocaleString()}개 · 개정 이력 ${data.criteria.revisions.toLocaleString()}건 수록`,...stateNotes()]:stateNotes());return}
  const products=productTerms(terms);
  // 'short' notes the two-character minimum; 'none' is a date or notice-number query, which names no product.
  const productState=products.length?'loading':terms.some(term=>Array.from(term).length<2)?'short':'none';
  view={query:terms.join(' '),criteria:groupCriteria(matching(data.criteria.rows,terms),terms),
        products:{state:productState},documents:matching(data.documents.rows,terms)};
  if(view.criteria.length)root.append(criteriaNode(view.criteria));
  const documents=documentsNode(view.documents);if(documents)root.append(documents);
  renderStatus();
  if(products.length)showProducts(view,products);
}
function placeSection(id,node,before){
  document.getElementById(id)?.remove();
  if(node)root.insertBefore(node,before?document.getElementById(before):null);
  renderStatus();
}
// Responses for a query no longer on screen are dropped.
function showProducts(current,terms){
  productsFor(terms).then(result=>{
    if(view!==current)return;
    current.products={state:'ready',...result};
    placeSection('sec-mfds',result.matches.length?mfdsNode(result.index,result.matches):null,'sec-docs');
  },error=>{
    if(view!==current)return;
    current.products={state:error instanceof StaleBuild?'stale':'failed'};
    placeSection('sec-mfds',null);console.error(error);
  });
}
// Called when the documents index finishes loading.
function refreshDocuments(){
  const terms=queryTerms();
  if(!view||view.query!==terms.join(' ')){render();return}
  view.documents=matching(data.documents.rows,terms);
  placeSection('sec-docs',documentsNode(view.documents));
}
const initialQuery=new URLSearchParams(location.search).get('q');if(initialQuery){q.value=initialQuery;intro.hidden=true}
const syncUrl=()=>{const term=q.value.trim();history.replaceState(null,'',term?`?q=${encodeURIComponent(term)}`:location.pathname)};
let renderTimer=null;const scheduleRender=()=>{clearTimeout(renderTimer);renderTimer=setTimeout(render,150)};
const getJson=(url,options)=>fetch(url,options).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()});
// Product files of one build only: a file from another build would pair rows with the wrong row numbers.
const mfdsBuild=__MFDS_BUILD__;
class StaleBuild extends Error{}
const checkBuild=file=>{if(file.build!==mfdsBuild)throw new StaleBuild(`product data build ${file.build}`);return file};
// Bounded caches: a phone browsing many queries should not keep every block it ever loaded.
const MFDS_FILE_CACHE=200,PRODUCT_QUERY_CACHE=8;
const remember=(cache,key,value,limit)=>{cache.set(key,value);if(cache.size>limit)cache.delete(cache.keys().next().value);value.catch(()=>cache.delete(key))};
const mfdsFiles=new Map();
function loadMfds(path){
  if(!mfdsFiles.has(path))remember(mfdsFiles,path,getJson(`mfds/${path}?v=${mfdsBuild}`).then(checkBuild),MFDS_FILE_CACHE);
  return mfdsFiles.get(path);
}
// The full product index (~1MB) is fetched once, and only when a query needs too many blocks.
function fullIndex(){
  if(!data.mfds.promise){
    pauseDocuments();data.mfds.state='loading';
    data.mfds.promise=getJson(`mfds/search-index.json?v=${mfdsBuild}`).then(checkBuild).then(raw=>{
      data.mfds={full:buildMfdsIndex(raw),promise:data.mfds.promise,state:'ready'};ensureDocuments();return data.mfds.full;
    },error=>{data.mfds={full:null,promise:null,state:'failed'};ensureDocuments();throw error});
  }
  return data.mfds.promise;
}
const productQueries=new Map();
function productsFor(terms){
  if(data.mfds.full)return Promise.resolve({index:data.mfds.full,matches:matchMfds(data.mfds.full,terms)});
  const key=terms.join(' ');
  if(!productQueries.has(key))remember(productQueries,key,productResults(terms,loadMfds,fullIndex),PRODUCT_QUERY_CACHE);
  return productQueries.get(key);
}
// Load order: criteria first (a ?q= link also starts its product search, whose files are small), related
// documents last.
if(initialQuery){const terms=productTerms(searchTerms(initialQuery));if(terms.length)productsFor(terms).catch(()=>{})}
getJson('search-index.json').then(raw=>{
  const rows=raw.map(searchableCriterion),latest=new Map();
  // Latest revision and revision count per criterion; results list only the revisions that matched.
  const revisionsOf=new Map();
  for(const record of rows){
    const current=latest.get(record.key);if(!current||byNewest(record,current)<0)latest.set(record.key,record);
    revisionsOf.set(record.key,(revisionsOf.get(record.key)||0)+1);
  }
  data.criteria={rows,latest,revisionsOf,revisions:rows.length,state:'ready'};render(true);
}).catch(e=>{data.criteria={rows:[],latest:new Map(),revisionsOf:new Map(),revisions:0,state:'failed'};render(true);console.error(e)}).then(ensureDocuments);
// Documents never share bandwidth with the full product index: they wait for it, and a download already
// running is dropped when the full index starts and restarted afterwards.
let documentsDownload=null;
function ensureDocuments(){
  if(data.documents.state!=='idle'||data.mfds.state==='loading')return;
  data.documents.state='loading';
  if(view)renderStatus();
  const download=documentsDownload=new AbortController();
  getJson('documents-index.json',{priority:'low',signal:download.signal}).then(raw=>{data.documents={rows:raw.map(searchableCriterion),state:'ready'};refreshDocuments()})
    .catch(e=>{if(download.signal.aborted)return;data.documents={rows:null,state:'failed'};refreshDocuments();console.error(e)});
}
function pauseDocuments(){
  if(data.documents.state!=='loading')return;
  documentsDownload.abort();
  data.documents.state='idle';
  if(view)renderStatus();
}
q.addEventListener('input',()=>{syncUrl();scheduleRender()});
intro.addEventListener('click',event=>{const button=event.target.closest('button[data-q]');if(!button)return;q.value=button.dataset.q;syncUrl();render()});
