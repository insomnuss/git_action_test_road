# hud-toll-data


```
원본 데이터  →  이 저장소(data/)  →  jsDelivr CDN  →  앱
```

## 데이터 출처 (현재는 수동 갱신)

한국도로공사 **"영업소간 통행요금 조회(FILE)"** 데이터셋을 쓴다.
([data.go.kr/data/15117921](https://www.data.go.kr/data/15117921/fileData.do))

- 승용차부터 대형화물까지 **1~8종 전 차종 정상요금**
- **시간대별 할인요금 2구간**(심야할인 등, 시작/종료 시분 포함)
- 한국도로공사 + 민자고속도로(서부내륙·경기·천안논산·부산울산·신대구부산 등) **전부 포함**
- 628개 영업소, 355,664개 구간

이 데이터셋은 **오픈API를 제공하지 않고 파일로만 배포**한다. 다운로드 페이지가 세션
기반 폼(활용목적 선택 등)으로 돼 있어 스크립트로 직접 받는 걸 시도했으나 막혔다
(`data.ex.co.kr`가 봇 요청은 400으로 차단하고, 브라우저 헤더를 흉내 내도 실제 파일이
아니라 다운로드 폼이 담긴 HTML만 내려온다). 그래서 지금은:

1. [다운로드 페이지](https://www.data.go.kr/data/15117921/fileData.do)에서 브라우저로 직접 받는다
2. `data/source/`에 그 파일을 놓는다 (파일명 아무거나 상관없음, 최신 mtime 파일을 자동으로 찾는다)
3. `scripts/build_toll_db.py`를 돌려 앱 배포용 산출물을 만든다

이 데이터는 **연 1~2회 정도만 갱신**되는 성격이라(현재 원본도 2025-08-01자 기준) 수동
방식으로도 운영에 지장이 없다. 원본 파일 자체도 `data/source/`에 커밋해두므로 빌드
과정이 재현 가능하다.

> 별도로 **OpenAPI 방식**(`scripts/update_toll.py` + 워크플로 `update-toll.yml`)도 이
> 저장소에 있다. [영업소간 통행요금 조회(OpenAPI)](https://www.data.go.kr/data/15111644/openapi.do)는
> 서비스키 활용신청만 하면 자동 페이징 수집이 가능한 별개 데이터셋이다. 다만 이 API가
> 위 FILE 데이터셋과 동일한 항목(할인 시간대 등)을 주는지 아직 실측하지 못했다 - 서비스키를
> 발급받아 한 번 돌려보면 어느 쪽을 자동화 기본으로 쓸지 정할 수 있다. 그 전까지는 파일
> 방식이 기준(source of truth)이다.

## 빌드 (원본 → 배포 산출물)

```bash
python scripts/build_toll_db.py
```

- `data/source/` 안의 최신 파일(확장자가 `.zip`이어도 실제로는 gzip)을 찾아 연다
- 필요한 컬럼만 뽑는다: 영업소 출발/도착, 운영기관, 1~8종 정상요금, 할인시간대 2구간
  (차로별 거리·주행시간·경로수 등 요금 표시에 불필요한 컬럼은 뺀다)
- `toll_fares.sqlite.gz` (인덱스 포함, 42MB → gzip 8MB대)와 `toll_fares.csv.gz`(4MB대)를 만든다
- `manifest.json`을 갱신한다

컬럼은 원본 헤더 **이름**으로 찾는다(순서 의존 없음). 원본 형식이 바뀌어 필요한 컬럼이
없으면 빌드가 즉시 실패한다 - 조용히 잘못된 데이터를 만드는 것보다 낫다.

## 저장소는 반드시 Public

jsDelivr는 공개 저장소만 서빙한다. 비공개면 CDN 배포가 되지 않는다.

## 산출물

| 파일 | 용도 |
|---|---|
| `data/manifest.json` | 버전·해시·다운로드 URL. 앱이 매번 이것만 먼저 받는다 |
| `data/toll_fares.sqlite.gz` | **앱이 실제로 받는 파일.** 압축 해제하면 인덱스 포함 SQLite |
| `data/toll_fares.csv.gz` | 사람이 보거나 다른 도구에서 쓸 CSV |
| `data/source/*` | 원본 파일(재현용, 커밋됨) |

`manifest.json` 예시:

```json
{
  "version": "data-20260816-1700",
  "rows": 355664,
  "columns": ["start_code", "start_name", ..., "d1_fare1", "d1_start", "d1_end", ...],
  "files": [{
    "name": "toll_fares.sqlite.gz",
    "bytes": 8321655,
    "uncompressed_bytes": 42049536,
    "sha256": "7678e874...",
    "url": "https://cdn.jsdelivr.net/gh/<user>/<repo>@data-20260816-1700/data/toll_fares.sqlite.gz"
  }]
}
```

## 앱 쪽 갱신 흐름

```
앱 시작
  → manifest.json 받기 (수백 바이트)
  → 저장된 version/sha256과 비교
     같으면  → 아무것도 안 받음 (대부분의 경우)
     다르면  → manifest 안의 url로 sqlite.gz 받아 압축 해제 후 교체
```

## 캐시 지연을 어떻게 피하는가

jsDelivr는 브랜치(`@main`) 경로를 **12시간** 캐싱한다. 파일을 갱신해도 한동안 옛 파일이
내려가는 문제가 여기서 생긴다. 이 저장소는 두 가지로 해결한다.

- **데이터 파일은 태그 URL(`@data-YYYYMMDD-HHMM`)로 고정**한다. 태그는 불변이라 jsDelivr가
  영구 캐싱하고, 새 데이터는 새 태그 = 새 URL이므로 애초에 캐시가 겹치지 않는다.
- **manifest만 `@main`으로 두고**, OpenAPI 자동 워크플로를 쓸 경우 갱신 시 jsDelivr purge
  API를 호출해 캐시를 비운다. 수동 빌드 시에는 필요하면 다음 주소로 직접 호출한다:
  `https://purge.jsdelivr.net/gh/<user>/<repo>@main/data/manifest.json`

즉 앱 코드에 버전을 하드코딩하고 스토어 업데이트를 하는 방식이 필요 없다.

## OpenAPI 자동화 (검증 완료)

`scripts/update_toll.py` + `.github/workflows/update-toll.yml`이 매주 월요일
03:00(KST) 자동 실행되도록 만들어져 있다. `scripts/build_toll_db.py`(파일 방식)와
`scripts/toll_schema.py`를 공유해서 **완전히 같은 컬럼의 sqlite.gz/csv.gz/manifest.json**을
만든다 - 앱은 데이터가 어느 파이프라인에서 나왔는지 몰라도 된다.

### 실제 API 위치 (data.go.kr이 아니라 data.ex.co.kr)

data.go.kr의 [15111644](https://www.data.go.kr/data/15111644/openapi.do)는
**링크형(Link API)**이다 - data.go.kr이 직접 프록시하지 않고, 실제로는 한국도로공사
자체 포털(OpenOASIS, data.ex.co.kr)로 안내한다. 실제 요청 URL과 파라미터는 그 포털의
API 상세 페이지(`data.ex.co.kr/openapi/basicinfo/openApiInfoM?apiId=0620`)에서 확인했고,
실제 서비스키로 호출해 검증했다 - 응답의 count가 355,664로 FILE 데이터셋과 완전히
일치한다(같은 데이터, 다른 배포 경로).

```
요청 URL : https://data.ex.co.kr/openapi/toll/bhoinstIntoTollList
파라미터 : key(인증키), type(json/xml), pageNo, numOfRows,
           그 외 선택: arrvTolofCd/Nm, dprtrTolofCd/Nm, hoinstCd
```

이 포털은 apis.data.go.kr 계열과 파라미터명이 다르다 - 인증키는 serviceKey가 아니라
key, 포맷은 _type이 아니라 type이다. scripts/update_toll.py는 이미 이 규칙으로 맞춰져
있다.

### 서버가 페이지 크기를 강제한다 (중요, 실제로 겪은 버그)

numOfRows를 1000으로 요청해도 서버는 실제로 99건만 돌려준다(응답의 numOfRows 필드
자체가 99로 온다). 처음엔 "요청 건수보다 적게 왔으면 마지막 페이지"로 페이징 종료를
판단했는데, 이 API에서는 모든 페이지가 항상 요청 건수보다 적게 오므로 매번 첫 페이지
에서 멈춰버리는 버그가 있었다 - 355,664건 중 99건만 받고 끝났을 뻔했다.

그래서 종료 조건을 응답의 count(전체 건수) 도달 여부로 바꿨다. 실측 기준 전체
페이지 수는 약 3,593페이지(99건 x 3,593 = 355,664)다.

### 필드 매칭도 검증했다

toll_schema.py의 FIELD_ALIASES는 이제 실제 확인된 필드명이 1순위다(dprtrTolofCd,
nrmlKnd1Amt 등 - 위 API 상세 페이지의 "출력결과" 표와 실제 호출 응답으로 대조 확인).
실제 호출로 34개 표준 컬럼 100% 매칭을 확인했다.

다만 API가 향후 필드명을 바꿀 가능성에 대비해 안전장치는 남겨뒀다 - 매칭률이 50%
미만이거나 필수 컬럼(start_code/end_code/fare1)이 비면 즉시 중단하고 실제 응답 키
목록을 로그에 남긴다. 그 목록을 보고 FIELD_ALIASES에 한 줄만 추가하면 된다.

매칭 판단은 값이 아니라 키의 존재 여부로 한다 - 통행료 데이터는 할인 시간대가 없는
구간처럼 정당한 null이 흔해서, 가장 첫 응답 행이 하필 그런 구간이면 값 기준 판단은
매칭이 정상인데도 실패로 오판한다. 실측 중 실제로 겪은 문제라 고쳤다.

### 쓰려면

1. [영업소간 통행요금 조회(OpenAPI)](https://www.data.go.kr/data/15111644/openapi.do)에서
   활용신청 → 서비스키 발급 (도로공사 자체 포털 계정/키 체계라 data.go.kr의 일반적인
   서비스키 발급 절차와 다를 수 있다)
2. 저장소 Settings → Secrets and variables → Actions → Secret TOLL_SERVICE_KEY 등록
3. Actions 탭 → "통행요금 데이터 갱신" → Run workflow로 실제 전체 수집 확인
   (약 3,593회 API 호출 - 시간이 좀 걸린다)


## 데이터가 그대로면 커밋하지 않는다

OpenAPI 워크플로는 `data/`에 실제 변경이 있을 때만 커밋·태그한다. 빈 커밋을 매주
쌓으면 앱이 매번 새 버전으로 오인해 불필요한 다운로드를 하게 된다.

## 실패 시 동작

수집/빌드 결과가 비어 있으면 스크립트가 실패로 끝나고 **기존 파일을 건드리지 않는다**.
반쯤 받다 만 데이터가 CDN에 올라가면 앱 전체가 잘못된 요금을 표시하게 되므로,
전부 처리된 뒤에만 파일을 교체한다.
