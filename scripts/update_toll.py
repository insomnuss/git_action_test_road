#!/usr/bin/env python3
"""
한국도로공사 영업소간 통행요금 조회 OpenAPI(data.go.kr/data/15111644)를 페이징 호출해
build_toll_db.py(파일 방식)와 동일한 sqlite.gz + csv.gz + manifest.json을 만든다.

두 파이프라인의 출력 스키마가 같으므로 앱은 어느 쪽이 만들었는지 몰라도 된다.

설계 메모
- 표준 라이브러리만 쓴다. CI에서 pip 설치 단계를 없애 빌드가 빠르고 덜 깨진다.
- 실제 API 응답 필드명은 서비스키를 발급받아 호출해보기 전까진 확정할 수 없다
  (공공데이터포털 페이지에 상세 스펙이 없음). 그래서 toll_schema.FIELD_ALIASES의
  후보 키들로 매칭을 시도하고, 처음 받은 행의 매칭률이 너무 낮으면 즉시 실패하면서
  실제로 들어온 키 목록을 그대로 로그에 남긴다. 그 로그를 보고 toll_schema.py의
  FIELD_ALIASES에 한 줄만 추가하면 다음 실행부터 정상 매칭된다 - 조용히 빈 칸투성이
  데이터를 커밋하는 사고를 막기 위함이다.
- 실패 시 기존 파일을 덮어쓰지 않는다(toll_schema.build_outputs_from_rows가 보장).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toll_schema import FIELD_ALIASES, REQUIRED_COLUMNS, build_outputs_from_rows, normalize_row  # noqa: E402

DEFAULT_API_URL = "https://apis.data.go.kr/B090041/openapi/service/TolInfoService/getTolInfo"

PAGE_SIZE = 1000
MAX_PAGES = 500
TIMEOUT_SEC = 30
RETRY = 3

# 첫 페이지 결과로 필드 매칭률을 판단한다. 표준 컬럼이 총 34개인데, 이 중 이만큼도
# 못 채우면 별칭 표가 안 맞는다고 보고 중단한다(REQUIRED_COLUMNS는 별도로 하나라도
# 비면 즉시 중단 - 그건 데이터를 아예 못 쓰는 수준이라서).
MIN_MATCH_RATIO = 0.5

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_page(base_url: str, service_key: str, page_no: int, extra: dict[str, str]) -> dict:
    params = {
        "serviceKey": service_key,
        "pageNo": str(page_no),
        "numOfRows": str(PAGE_SIZE),
        "_type": "json",
        **extra,
    }
    # serviceKey는 이미 URL 인코딩된 형태로 발급되는 경우가 많아 이중 인코딩을 피한다.
    query = "&".join(
        f"{k}={v if k == 'serviceKey' else urllib.parse.quote(str(v), safe='')}"
        for k, v in params.items()
    )
    url = f"{base_url}?{query}"

    last_err: Exception | None = None
    for attempt in range(1, RETRY + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hud-toll-updater/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if not raw.lstrip().startswith("{"):
                # 인증키 오류 등은 XML 에러 문서로 돌아온다. 그대로 노출해야 원인 파악이 된다.
                raise RuntimeError(f"JSON이 아닌 응답: {raw[:300]}")
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001 - 네트워크/파싱 모두 재시도 대상
            last_err = e
            if attempt < RETRY:
                wait = 2 ** attempt
                log(f"  page {page_no} 실패({e}) - {wait}s 후 재시도 {attempt}/{RETRY}")
                time.sleep(wait)
    raise RuntimeError(f"page {page_no} 조회 실패: {last_err}")


def extract_rows(payload: dict) -> list[dict]:
    """
    공공데이터포털 응답에서 행 목록을 꺼낸다. 기관마다 감싸는 구조가 달라서
    (response.body.items.item / list / data ...) 딕셔너리 리스트가 나오는 첫 지점을 찾는다.
    """
    def walk(node):
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                return node
            return None
        if isinstance(node, dict):
            for key in ("item", "items", "list", "data", "body", "response"):
                if key in node:
                    found = walk(node[key])
                    if found is not None:
                        return found
            for v in node.values():
                found = walk(v)
                if found is not None:
                    return found
        return None

    rows = walk(payload)
    if rows is None:
        body = payload.get("response", {}).get("body", {})
        item = body.get("items", {})
        if isinstance(item, dict) and isinstance(item.get("item"), dict):
            return [item["item"]]
        return []
    return rows


def check_first_batch_or_die(raw_rows: list[dict]) -> None:
    """
    첫 페이지로 별칭 매칭이 그럴듯한지 확인한다. 여기서 실패하면 나머지 34만행을
    받으러 다닐 필요가 없다 - 빨리 실패해서 CI 시간과 API 호출 쿼터를 아낀다.
    """
    if not raw_rows:
        raise RuntimeError("첫 페이지가 비어 있다 - 파라미터를 확인해라")

    sample = raw_rows[0]
    std, matched = normalize_row(sample)
    total = len(std)
    ratio = matched / total

    missing_required = [c for c in REQUIRED_COLUMNS if not std.get(c)]

    log(f"필드 매칭 진단: {matched}/{total} ({ratio:.0%}) - 실제 응답 키: {sorted(sample.keys())}")

    if missing_required or ratio < MIN_MATCH_RATIO:
        lines = [
            "필드 매칭률이 너무 낮아 중단한다 - toll_schema.py의 FIELD_ALIASES가 실제",
            "API 응답 필드명과 안 맞는 것으로 보인다.",
            "",
            f"필수 컬럼 중 비어 있음: {missing_required or '없음'}",
            f"전체 매칭률: {matched}/{total} ({ratio:.0%}, 최소 {MIN_MATCH_RATIO:.0%} 필요)",
            "",
            f"실제 응답의 첫 행 키 목록:\n  {sorted(sample.keys())}",
            "",
            "조치: 위 키 목록을 보고 scripts/toll_schema.py의 FIELD_ALIASES에서",
            "해당 컬럼에 실제 키 이름을 추가한 뒤 다시 실행해라.",
        ]
        raise RuntimeError("\n".join(lines))


def download_all(base_url: str, service_key: str, extra: dict[str, str]):
    """정규화된 표준 행을 순서대로 내어주는 제너레이터. 첫 페이지에서 매칭을 검증한다."""
    first_raw = fetch_page(base_url, service_key, 1, extra)
    first_rows = extract_rows(first_raw)
    check_first_batch_or_die(first_rows)

    total = 0
    for raw in first_rows:
        std, _ = normalize_row(raw)
        total += 1
        yield std
    log(f"  page 1: {len(first_rows)}건 (누적 {total})")

    if len(first_rows) < PAGE_SIZE:
        return

    for page in range(2, MAX_PAGES + 1):
        payload = fetch_page(base_url, service_key, page, extra)
        raw_rows = extract_rows(payload)
        if not raw_rows:
            log(f"  page {page}: 0건 - 수집 종료")
            return
        for raw in raw_rows:
            std, _ = normalize_row(raw)
            total += 1
            yield std
        log(f"  page {page}: {len(raw_rows)}건 (누적 {total})")
        if len(raw_rows) < PAGE_SIZE:
            return
        time.sleep(0.2)  # 서버 부담을 줄이는 최소한의 간격
    log(f"  경고: MAX_PAGES({MAX_PAGES}) 도달 - 데이터가 더 있을 수 있다")


def main() -> int:
    service_key = os.environ.get("TOLL_SERVICE_KEY", "").strip()
    if not service_key:
        log("TOLL_SERVICE_KEY 환경변수가 없다. GitHub Secrets에 등록해야 한다.")
        return 2

    base_url = os.environ.get("TOLL_API_URL", DEFAULT_API_URL).strip()
    extra_raw = os.environ.get("TOLL_API_PARAMS", "").strip()
    extra = dict(urllib.parse.parse_qsl(extra_raw)) if extra_raw else {}

    repo = os.environ.get("GITHUB_REPOSITORY", "insomnuss/git_action_test_road").strip()
    tag = os.environ.get("DATA_TAG", "").strip()

    log(f"통행요금 수집 시작(OpenAPI): {base_url}")
    if extra:
        log(f"  추가 파라미터: {extra}")

    try:
        manifest = build_outputs_from_rows(
            download_all(base_url, service_key, extra),
            DATA_DIR,
            source_label="한국도로공사 영업소간 통행요금 조회 (OpenAPI, data.go.kr/data/15111644)",
            source_file_label=f"{base_url} (자동 수집)",
            tag=tag,
            repo=repo,
        )
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log(f"수집 실패: {e}")
        return 1

    log(f"완료: {manifest['rows']:,}건")
    for f in manifest["files"]:
        log(f"{f['name']:22} {f['bytes']:>12,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
