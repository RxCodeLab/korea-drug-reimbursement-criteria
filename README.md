# 약제 급여기준 변경 이력 검색

건강보험 약제 급여기준(보건복지부 고시 「요양급여의 적용기준 및 방법에 관한 세부사항(약제)」)을 성분명이나 제품명으로 찾고, 시행일별로 무엇이 신설·변경·삭제되었는지 확인하는 검색 서비스입니다. 약제별 보험 인정기준과 허가사항 초과 사용 시 급여 기준, 약값 전액 본인부담 기준을 개정 이력과 함께 볼 수 있습니다. 식약처 허가 품목의 효능·효과(허가 적응증)와 그 변경 이력도 함께 조회됩니다.

**[검색 서비스 열기](https://rxcodelab.github.io/korea-drug-reimbursement-criteria/)**

> 검색 결과는 원문을 가공한 참고자료입니다. 실제 급여 적용이나 청구를 판단할 때는 최신 고시 원문을 확인하세요.

*English:* Search Korea's National Health Insurance drug reimbursement criteria (the Ministry of Health and Welfare notice on drug-specific coverage criteria, also referred to as HIRA drug reimbursement standards) by ingredient or brand name. Each criterion shows its full revision history with effective dates and notice numbers, alongside the MFDS (Ministry of Food and Drug Safety) approved indications of matching products and how those indications changed. Data is collected six days a week from official open APIs and is also published as a SQLite snapshot.

## 검색할 수 있는 내용

- 급여기준: 약제별 기준 본문, 신설·변경·삭제 이력, 시행일, 고시번호
- 관련 고시문과 첨부자료: 고시 개정문, 개정 이유, 변경 대비표, 질의응답
- 식약처 허가 품목: 효능·효과와 그 변경 이력. 건강보험 급여 여부와 관계없이 허가 품목 전체를 싣습니다.

## 검색 방법

검색창에 성분명(한글·영문), 제품명, 시행일, 고시번호를 입력합니다.

```text
다파글리플로진
dapagliflozin
포시가
2026-06-01
고시 제2026-117호
```

입력한 검색어를 그대로 포함한 항목을 찾습니다. 제목이나 성분명이 맞는 급여기준이 먼저 나오고 같은 기준의 개정은 최신 시행일부터 표시합니다.

## 약제별 페이지

급여기준마다 개정 전문을 시행일 순으로 모은 페이지가 있습니다. 성분명과 허가 품목, 함께 적용되는 일반원칙, 같은 분류의 다른 기준으로 이어지는 링크도 들어 있습니다.

- [Dapagliflozin 경구제 (다파글리플로진)](https://rxcodelab.github.io/korea-drug-reimbursement-criteria/criteria/396-dapagliflozin%EA%B2%BD%EA%B5%AC%EC%A0%9C.html)
- [Rivaroxaban 경구제 (리바록사반)](https://rxcodelab.github.io/korea-drug-reimbursement-criteria/criteria/333-rivaroxaban%EA%B2%BD%EA%B5%AC%EC%A0%9C.html)
- [당뇨병용제 일반원칙](https://rxcodelab.github.io/korea-drug-reimbursement-criteria/criteria/%EC%9D%BC%EB%B0%98%EC%9B%90%EC%B9%99-%EB%8B%B9%EB%87%A8%EB%B3%91%EC%9A%A9%EC%A0%9C.html)

전체 목록은 검색 페이지 아래 「수록된 약제 급여기준 목록」에 약효분류별로 정리되어 있습니다.

## 자료 출처와 갱신

- 급여기준: 법제처 국가법령정보센터 Open API
- 허가 품목: 공공데이터포털 식약처 의약품 제품 허가정보 API, 의약품안전나라 변경 이력

일요일을 제외한 매일 저녁 새 고시와 허가 변경을 확인하고 검증을 통과한 자료만 반영합니다. 갱신 중 문제가 발견되면 마지막으로 정상 확인된 자료를 유지합니다. 검색 페이지 아래에는 최근 고시 날짜와 마지막으로 수집에 성공한 날짜를 표시합니다. 상위 기관 수집이 실패한 날은 이 저장소에 `upstream-failure` 라벨을 붙인 issue로 기록합니다.

## 데이터 내려받기

검증을 통과한 자료는 [Releases](https://github.com/RxCodeLab/korea-drug-reimbursement-criteria/releases)에 SQLite 스냅샷(`criteria.db`)으로도 공개됩니다. 최신 파일은 [criteria.db](https://github.com/RxCodeLab/korea-drug-reimbursement-criteria/releases/latest/download/criteria.db)에서 바로 받을 수 있습니다.

`versions`·`attachments`·`entries` 테이블을 쓸 수 있고 `entries`의 제목·본문을 색인한 FTS5 가상 테이블 `fts`로 전문 검색도 할 수 있습니다.

```sql
SELECT rowid FROM fts WHERE fts MATCH '다파글리플로진';
```
