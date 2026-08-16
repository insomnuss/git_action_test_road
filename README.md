# hud-toll-data

HUD Cockpit 앱이 쓰는 **고속도로 통행요금 데이터** 저장소.

서버 없이 운영한다. GitHub Actions가 주기적으로 공공데이터포털에서 요금을 받아
이 저장소에 커밋하고, 앱은 jsDelivr CDN으로 그 결과를 받아간다.

```
공공데이터포털 API  →  GitHub Actions(주 1회)  →  이 저장소  →  jsDelivr CDN  →  앱
```

## 준비 (최초 1회)

1. **공공데이터포털 활용신청**
   [한국도로공사_영업소간 통행요금 조회](https://www.data.go.kr/data/15111644/openapi.do)에서
   활용신청 → 서비스키 발급. 무료이며 별도 제휴 승인은 필요 없다.

2. **저장소 시크릿 등록**
   Settings → Secrets and variables → Actions
   - Secret `TOLL_SERVICE_KEY` : 발급받은 서비스키 (필수)
   - Variable `TOLL_API_URL` : 실제 엔드포인트가 스크립트 기본값과 다를 때만 (선택)
   - Variable `TOLL_API_PARAMS` : 추가 쿼리 파라미터, `a=1&b=2` 형식 (선택)

3. **저장소는 반드시 Public**
   jsDelivr는 공개 저장소만 서빙한다. 비공개면 CDN 배포가 되지 않는다.

## 수동 실행

Actions 탭 → "통행요금 데이터 갱신" → Run workflow.
첫 실행으로 엔드포인트/파라미터가 맞는지 확인한 뒤 스케줄에 맡기면 된다.

## 산출물

| 파일 | 용도 |
|---|---|
| `data/manifest.json` | 버전·해시·다운로드 URL. 앱이 매번 이것만 먼저 받는다 |
| `data/toll_fares.csv` | 실제 요금 데이터 |

`manifest.json` 예시:

```json
{
  "version": "data-20260816-0300",
  "generated_at": "2026-08-16T03:00:12+09:00",
  "files": [{
    "name": "toll_fares.csv",
    "rows": 12345,
    "sha256": "56bfe0ff...",
    "url": "https://cdn.jsdelivr.net/gh/<user>/<repo>@data-20260816-0300/data/toll_fares.csv"
  }]
}
```

## 앱 쪽 갱신 흐름

```
앱 시작
  → manifest.json 받기 (수백 바이트)
  → 저장된 version/sha256과 비교
     같으면  → 아무것도 안 받음 (대부분의 경우)
     다르면  → manifest 안의 url로 CSV 받아 교체
```

## 캐시 지연을 어떻게 피하는가

jsDelivr는 브랜치(`@main`) 경로를 **12시간** 캐싱한다. 파일을 갱신해도 한동안 옛 파일이
내려가는 문제가 여기서 생긴다. 이 저장소는 두 가지로 해결한다.

- **데이터 CSV는 태그 URL(`@data-YYYYMMDD-HHMM`)로 고정**한다. 태그는 불변이라 jsDelivr가
  영구 캐싱하고, 새 데이터는 새 태그 = 새 URL이므로 애초에 캐시가 겹치지 않는다.
- **manifest만 `@main`으로 두고**, 갱신 시 워크플로가 jsDelivr purge API를 호출해 캐시를 비운다.
  purge가 실패해도 최대 12시간 뒤 자연 갱신되므로 치명적이지 않다.

즉 앱 코드에 버전을 하드코딩하고 스토어 업데이트를 하는 방식이 필요 없다.

## 데이터가 그대로면 커밋하지 않는다

워크플로는 `data/`에 실제 변경이 있을 때만 커밋·태그한다. 빈 커밋을 매주 쌓으면
앱이 매번 새 버전으로 오인해 불필요한 다운로드를 하게 된다.

## 실패 시 동작

수집 결과가 0건이면 스크립트가 실패로 끝나고 **기존 파일을 건드리지 않는다**.
반쯤 받다 만 데이터가 CDN에 올라가면 앱 전체가 잘못된 요금을 표시하게 되므로,
전부 받은 뒤에만 파일을 교체한다.
