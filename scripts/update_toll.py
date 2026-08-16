#!/usr/bin/env python3
"""
한국도로공사 영업소간 통행요금 데이터를 공공데이터포털에서 받아 CSV + manifest로 저장한다.

GitHub Actions에서 주기적으로 돌리고, 결과 파일을 jsDelivr CDN으로 앱에 배포하는 것이 목적이다.
앱은 manifest.json만 먼저 받아 버전/해시를 비교하고, 바뀌었을 때만 CSV를 내려받는다.

설계 메모
- 표준 라이브러리만 쓴다. CI에서 pip 설치 단계를 없애 빌드가 빠르고 덜 깨진다.
- 응답 스키마를 미리 고정하지 않는다. 공공데이터포털 API 상세 규격이 활용신청 후에만
  공개되는데, 필드명을 잘못 박아두면 조용히 빈 데이터를 만들게 된다. 그래서 응답에 들어온
  키를 모아서 CSV 컬럼을 구성한다 - 규격을 몰라도 데이터가 통째로 보존된다.
- 실패 시 기존 파일을 덮어쓰지 않는다. 반쯤 받다 만 데이터가 CDN에 올라가면
  앱 전체가 잘못된 요금을 보게 되므로, 전부 받은 뒤에만 파일을 교체한다.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── 설정 ────────────────────────────────────────────────────────────────
# 활용신청 후 받은 실제 엔드포인트로 바꾸거나 TOLL_API_URL 환경변수로 넘긴다.
# 공공데이터포털 "한국도로공사_영업소간 통행요금 조회" (data.go.kr/data/15111644)
DEFAULT_API_URL = "https://apis.data.go.kr/B090041/openapi/service/TolInfoService/getTolInfo"

PAGE_SIZE = 1000        # 공공데이터포털 표준 상한
MAX_PAGES = 200         # 무한 루프 방지용 안전장치
TIMEOUT_SEC = 30
RETRY = 3

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_page(base_url: str, service_key: str, page_no: int, extra: dict[str, str]) -> dict:
    """한 페이지를 받아 JSON으로 돌려준다. 일시적 실패는 지수 백오프로 재시도."""
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
    공공데이터포털 응답에서 행 목록을 꺼낸다.
    기관마다 감싸는 구조가 달라서(response.body.items.item / list / data ...)
    딕셔너리 리스트가 나오는 첫 지점을 찾아 쓴다.
    """
    def walk(node):
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                return node
            return None
        if isinstance(node, dict):
            # item이 단일 객체로 오는 경우도 있다.
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
        # 단일 item이 dict인 경우
        body = payload.get("response", {}).get("body", {})
        item = body.get("items", {})
        if isinstance(item, dict) and isinstance(item.get("item"), dict):
            return [item["item"]]
        return []
    return rows


def download_all(base_url: str, service_key: str, extra: dict[str, str]) -> list[dict]:
    all_rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        payload = fetch_page(base_url, service_key, page, extra)
        rows = extract_rows(payload)
        if not rows:
            log(f"  page {page}: 0건 - 수집 종료")
            break
        all_rows.extend(rows)
        log(f"  page {page}: {len(rows)}건 (누적 {len(all_rows)})")
        if len(rows) < PAGE_SIZE:
            break
        time.sleep(0.2)  # 서버 부담을 줄이는 최소한의 간격
    else:
        log(f"  경고: MAX_PAGES({MAX_PAGES}) 도달 - 데이터가 더 있을 수 있다")
    return all_rows


def write_csv(rows: list[dict], path: Path) -> list[str]:
    """모든 행의 키를 합쳐 컬럼을 만든다(응답 스키마를 몰라도 손실 없이 저장)."""
    columns: list[str] = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in columns})
    return columns


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    service_key = os.environ.get("TOLL_SERVICE_KEY", "").strip()
    if not service_key:
        log("TOLL_SERVICE_KEY 환경변수가 없다. GitHub Secrets에 등록해야 한다.")
        return 2

    base_url = os.environ.get("TOLL_API_URL", DEFAULT_API_URL).strip()
    extra_raw = os.environ.get("TOLL_API_PARAMS", "").strip()
    extra = dict(urllib.parse.parse_qsl(extra_raw)) if extra_raw else {}

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()      # 예: user/repo
    tag = os.environ.get("DATA_TAG", "").strip()                # 예: data-20260816-0300

    log(f"통행요금 수집 시작: {base_url}")
    if extra:
        log(f"  추가 파라미터: {extra}")

    rows = download_all(base_url, service_key, extra)
    if not rows:
        # 빈 결과로 기존 파일을 날리면 앱이 요금을 못 쓰게 된다. 실패로 처리해 커밋을 막는다.
        log("수집된 데이터가 0건이다. 기존 파일을 유지하고 실패로 종료한다.")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DATA_DIR / "toll_fares.csv"
    columns = write_csv(rows, csv_path)

    digest = sha256_of(csv_path)
    size = csv_path.stat().st_size
    now = datetime.now(timezone.utc).astimezone()

    # 앱은 이 manifest만 먼저 받아 version/sha256을 비교하고, 바뀐 경우에만 CSV를 받는다.
    # 데이터 파일 URL은 태그로 고정해 jsDelivr가 영구 캐싱하게 한다(캐시 지연 문제 회피).
    ref = tag if tag else "main"
    cdn_base = f"https://cdn.jsdelivr.net/gh/{repo}@{ref}" if repo else ""
    manifest = {
        "version": tag or now.strftime("%Y%m%d-%H%M"),
        "generated_at": now.isoformat(timespec="seconds"),
        "source": "한국도로공사 영업소간 통행요금 (공공데이터포털)",
        "files": [
            {
                "name": "toll_fares.csv",
                "rows": len(rows),
                "columns": columns,
                "bytes": size,
                "sha256": digest,
                "url": f"{cdn_base}/data/toll_fares.csv" if cdn_base else "",
            }
        ],
    }
    (DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    log(f"완료: {len(rows)}건, {size:,} bytes, sha256={digest[:16]}...")
    log(f"컬럼({len(columns)}): {', '.join(columns[:12])}{' ...' if len(columns) > 12 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
