// assets/search-core.js 검증. `node --test tests/*.test.js`로 실행한다(외부 의존성 없음).
// DOM을 만지지 않는 부분만 core에 있으므로 이 파일은 document 없이 그대로 돌아간다.
const test = require('node:test');
const assert = require('node:assert');
const core = require('../assets/search-core.js');

const FIELDS = ['item_seq', 'item_name', 'entp_name', 'main_item_ingr',
                'main_item_ingr_eng', 'permit_date', 'revision_count', 'content_key'];

// build_site.encode_mfds_index가 내보내는 형태(사전 + 정수 인덱스) 그대로.
const packed = () => ({
  fields: FIELDS,
  dicts: {
    entp_name: ['시험제약', '다른제약'],
    main_item_ingr: ['[M1]다파글리플로진', '[M2]메트포르민'],
    main_item_ingr_eng: ['Dapagliflozin', 'Metformin'],
    permit_date: ['20200101', '20210202'],
    content_key: ['a'.repeat(12), 'b'.repeat(12)],
  },
  rows: [['1', '다파진정10밀리그램(다파글리플로진)', 0, 0, 0, 0, 2, 0],
         ['2', '무관정', 1, 1, 1, 1, 1, 1]],
});

// mfdsTier는 materializeMfds가 만든 객체에 exactKey를 붙여 쓴다(계층 정의의 기준 구현).
const withExactKey = item => ({ ...item,
  exactKey: [item.entp_name, item.main_item_ingr, item.main_item_ingr_eng].join('|').toLocaleLowerCase('ko') });
const mfds = (row = 0) => withExactKey(core.materializeMfds(core.buildMfdsIndex(packed()), row, 0));

test('brandName은 용량·괄호를 떼고 소문자 상표명만 남긴다', () => {
  assert.equal(core.brandName('다파진정10밀리그램(다파글리플로진)'), '다파진정');
  assert.equal(core.brandName('슈글렛정50밀리그램(이프라글리플로진L-프롤린)'), '슈글렛정');
  assert.equal(core.brandName('ABC Tab 5mg'), 'abc tab');
  // 용량 표기가 없으면 이름이 그대로 남는다.
  assert.equal(core.brandName('무관정'), '무관정');
});

test('dateLabel은 YYYYMMDD를 하이픈 형식으로 바꾼다', () => {
  assert.equal(core.dateLabel('20260901'), '2026-09-01');
});

test('mfdsTier는 상표 완전일치부터 무관까지 순위를 매긴다', () => {
  const item = mfds();
  assert.equal(core.mfdsTier(item, ['다파진정']), 0);        // 상표 완전일치
  assert.equal(core.mfdsTier(item, ['다파진']), 1);          // 상표 접두일치
  assert.equal(core.mfdsTier(item, ['10밀리그램']), 2);      // 품목명 부분일치
  assert.equal(core.mfdsTier(item, ['dapagliflozin']), 3);   // 제조사·성분 부분일치
  assert.equal(core.mfdsTier(item, ['zzz']), 4);             // 무관
  // 여러 검색어 중 하나라도 걸리면 가장 좋은 순위를 준다(안내된 OR 검색).
  assert.equal(core.mfdsTier(item, ['zzz', '다파진정']), 0);
});

test('buildMfdsIndex는 객체를 만들지 않고 열 배열과 검색 키만 준비한다', () => {
  const idx = core.buildMfdsIndex(packed());
  assert.equal(idx.n, 2);
  assert.deepEqual(idx.names, ['다파진정10밀리그램(다파글리플로진)', '무관정']);
  assert.deepEqual(idx.brands, ['다파진정', '무관정']);
  // 사전은 그대로 들고 행에는 정수 id만 남는다.
  assert.deepEqual(idx.entp.table, ['시험제약', '다른제약']);
  assert.deepEqual([...idx.entp.ids], [0, 1]);
  assert.deepEqual(idx.ingrEngLow, ['dapagliflozin', 'metformin']);
});

test('buildMfdsIndex는 사전이 없는 옛 형식 색인도 읽는다', () => {
  const plain = packed();
  const restored = plain.rows.map(row => row.slice());
  restored[0][2] = '시험제약'; restored[1][2] = '다른제약';
  restored[0][3] = '[M1]다파글리플로진'; restored[1][3] = '[M2]메트포르민';
  restored[0][4] = 'Dapagliflozin'; restored[1][4] = 'Metformin';
  restored[0][5] = restored[1][5] = '20200101';
  restored[0][7] = 'a'.repeat(12); restored[1][7] = 'b'.repeat(12);
  const idx = core.buildMfdsIndex({ fields: plain.fields, rows: restored });
  assert.deepEqual(idx.entp.table, ['시험제약', '다른제약']);
  assert.deepEqual([...idx.entp.ids], [0, 1]);
});

test('matchMfds는 계층 순으로 행 번호를 돌려주고 무관한 행은 뺀다', () => {
  const idx = core.buildMfdsIndex(packed());
  assert.deepEqual(core.matchMfds(idx, ['다파진정']), [[0, 0]]);          // 상표 완전일치
  assert.deepEqual(core.matchMfds(idx, ['다파진']), [[1, 0]]);            // 상표 접두일치
  assert.deepEqual(core.matchMfds(idx, ['10밀리그램']), [[2, 0]]);        // 품목명 부분일치
  assert.deepEqual(core.matchMfds(idx, ['metformin']), [[3, 1]]);         // 성분 부분일치
  assert.deepEqual(core.matchMfds(idx, ['zzz']), []);                     // 무관
  assert.deepEqual(core.matchMfds(idx, []), []);
  // 여러 검색어는 OR이고, 결과는 계층 오름차순으로 정렬된다.
  assert.deepEqual(core.matchMfds(idx, ['metformin', '다파진정']), [[0, 0], [3, 1]]);
});

test('matchMfds는 mfdsTier와 같은 계층을 낸다', () => {
  const idx = core.buildMfdsIndex(packed());
  for (const terms of [['다파진정'], ['다파진'], ['10밀리그램'], ['dapagliflozin'], ['zzz'], ['시험제약']]) {
    const viaScan = new Map(core.matchMfds(idx, terms).map(([tier, row]) => [row, tier]));
    for (let row = 0; row < idx.n; row++) {
      const expected = core.mfdsTier(withExactKey(core.materializeMfds(idx, row, 0)), terms);
      assert.equal(viaScan.has(row) ? viaScan.get(row) : 4, expected, `terms=${terms} row=${row}`);
    }
  }
});

test('materializeMfds는 그릴 때만 품목 객체를 만든다', () => {
  const idx = core.buildMfdsIndex(packed());
  const item = core.materializeMfds(idx, 0, 2);
  assert.equal(item.item_seq, '1');
  assert.equal(item.entp_name, '시험제약');
  assert.equal(item.main_item_ingr, '[M1]다파글리플로진');
  assert.equal(item.permit_date, '20200101');
  assert.equal(item.content_key, 'a'.repeat(12));
  assert.equal(item.source_url, 'https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=1');
  assert.equal(item.tier, 2);   // 계층은 다시 계산하지 않고 그대로 실린다
});

test('indicationGroups는 성분으로 묶고 같은 효능·효과끼리 다시 묶는다', () => {
  const index = core.buildMfdsIndex({
    fields: FIELDS,
    dicts: { entp_name: ['제약갑'], main_item_ingr: ['성분A', ''], main_item_ingr_eng: ['IngredientA'],
             permit_date: ['20200202', '20200101'], content_key: ['k1', 'k2', 'k3'] },
    rows: [['1', '가정', 0, 0, 0, 0, 1, 0],
           ['2', '나정', 0, 0, 0, 1, 1, 0],
           ['3', '다정', 0, 0, 0, 0, 1, 1],
           ['4', '라정', 0, 1, 0, 0, 1, 2]],
  });
  const groups = core.indicationGroups(index, [[0, 0], [0, 1], [0, 2], [0, 3]]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].ingredient, '성분A');
  assert.equal(groups[0].groups.length, 2);
  // 같은 content_key끼리 묶이고 허가일 오름차순으로 정렬된다(나정 20200101 < 가정 20200202).
  assert.deepEqual(groups[0].groups[0].map(m => index.names[m[1]]), ['나정', '가정']);
  // 성분이 비면 '성분 미상'으로 묶인다.
  assert.equal(groups[1].ingredient, '성분 미상');
});

test('indicationGroups는 관련도 순서를 유지한다', () => {
  const index = core.buildMfdsIndex(packed());
  // 계층이 좋은 행(1번)이 먼저 오면 그 성분이 먼저 나온다.
  const groups = core.indicationGroups(index, [[0, 1], [3, 0]]);
  assert.deepEqual(groups.map(g => g.ingredient), ['[M2]메트포르민', '[M1]다파글리플로진']);
});

test('searchableCriterion은 제목·본문·고시번호를 한 문자열로 합친다', () => {
  const record = core.searchableCriterion({
    title: 'Dapagliflozin 경구제', body: '제2형 당뇨병', class_header: '[219] 당뇨병용제',
    effective_date: '20260601', notice_number: '2026-117',
  });
  assert.ok(record.hay.includes('dapagliflozin'));   // 소문자화된다
  assert.ok(record.hay.includes('2026-06-01'));      // 하이픈 날짜로도 찾힌다
  assert.ok(record.hay.includes('고시 제2026-117호'));
});

test('groupsBy는 등장 순서를 지키며 필드로 묶는다', () => {
  const groups = core.groupsBy([{ k: 'b', n: 1 }, { k: 'a', n: 2 }, { k: 'b', n: 3 }], 'k');
  assert.deepEqual(groups.map(g => g.map(x => x.n)), [[1, 3], [2]]);
});

test('MFDS_GROUP_LIMIT은 한 번에 그리는 품목 그룹 수를 제한한다', () => {
  assert.equal(typeof core.MFDS_GROUP_LIMIT, 'number');
  assert.ok(core.MFDS_GROUP_LIMIT > 0);
});
