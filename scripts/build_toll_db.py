#!/usr/bin/env python3
"""
한국도로공사 "영업소간 통행요금 조회(FILE)" 원본을 앱 배포용으로 변환한다.

원본은 CP949 CSV 83MB(35만행)라 그대로는 앱에 내려보낼 수 없다. 필요한 컬럼만 뽑아
toll_schema.build_outputs_from_rows()로 sqlite.gz + csv.gz + manifest.json을 만든다.
이 출력 형식은 update_toll.py(OpenAPI 방식)와 완전히 동일하다 - 앱은 어느 파이프라인이
만들었는지 몰라도 된다.

담는 것: 출발/도착 영업소, 운영기관, 1~8종 정상요금, 할인 시간대 2구간(1~8종 + 시작/종료)
빼는 것: 차로별 거리, 주행시간, 경로수 등 요금 계산에 불필요한 컬럼

컬럼은 이름으로 찾는다. 원본 컬럼 순서가 바뀌어도 깨지지 않게 하기 위함이다.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toll_schema import STANDARD_COLUMNS, TEXT_COLUMNS, build_outputs_from_rows, to_int  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SOURCE_DIR = DATA_DIR / "source"

# 원본 헤더명(정확히 일치) -> 표준 컬럼명. 파일 데이터셋은 우리가 직접 검증한 고정 헤더라
# toll_schema.py의 별칭 매칭(유사값 후보 여러 개)이 아니라 정확 매칭을 쓴다.
SOURCE_HEADER_MAP = {
    "출발영업소코드": "start_code",
    "출발영업소명": "start_name",
    "도착영업소코드": "end_code",
    "도착영업소명": "end_name",
    "고속도로운영기관코드": "operator_code",
    "고속도로운영기관명": "operator_name",
}
for _i in range(1, 9):
    SOURCE_HEADER_MAP[f"정상{_i}종금액"] = f"fare{_i}"
for _n, _prefix in ((1, "d1"), (2, "d2")):
    for _i in range(1, 9):
        SOURCE_HEADER_MAP[f"{_n}번째할인시간대할인{_i}종금액"] = f"{_prefix}_fare{_i}"
    SOURCE_HEADER_MAP[f"{_n}번째할인시간대할인시작시분"] = f"{_prefix}_start"
    SOURCE_HEADER_MAP[f"{_n}번째할인시간대할인종료시분"] = f"{_prefix}_end"


def log(msg: str) -> None:
    print(msg, flush=True)


def find_source() -> Path:
    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"원본 폴더가 없다: {SOURCE_DIR}")
    files = sorted(
        [p for p in SOURCE_DIR.iterdir() if p.is_file() and p.suffix.lower() in (".zip", ".gz", ".csv")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise SystemExit(f"원본 파일이 없다: {SOURCE_DIR}")
    return files[0]


def open_source(path: Path) -> io.TextIOWrapper:
    """
    원본을 텍스트로 연다. 공공데이터포털이 .zip 확장자로 주지만 실제 내용은 gzip이라
    확장자 대신 매직 넘버로 판별한다.
    """
    with path.open("rb") as f:
        magic = f.read(2)
    raw = gzip.open(path, "rb") if magic == b"\x1f\x8b" else path.open("rb")
    return io.TextIOWrapper(raw, encoding="cp949", errors="replace", newline="")


def iter_standard_rows(path: Path):
    with open_source(path) as fin:
        reader = csv.reader(fin)
        header = [h.strip().strip('"') for h in next(reader)]

        missing = [k for k in SOURCE_HEADER_MAP if k not in header]
        if missing:
            # 이 함수는 제너레이터라 이 코드는 build_outputs_from_rows()의
            # `for row in rows:` 안에서(첫 next() 호출 시) 실행된다. SystemExit은
            # BaseException이라 그쪽의 except Exception 정리 블록을 건너뛰어
            # sqlite 임시 파일이 Windows에서 잠긴 채 남는다 - RuntimeError를 쓴다.
            raise RuntimeError(f"원본에 없는 컬럼: {missing[:5]} ... (원본 형식이 바뀌었을 수 있다)")
        idx = {std_col: header.index(src_name) for src_name, std_col in SOURCE_HEADER_MAP.items()}

        for row in reader:
            if not row:
                continue
            out = {}
            for col in STANDARD_COLUMNS:
                raw = row[idx[col]] if idx[col] < len(row) else ""
                out[col] = raw.strip() if col in TEXT_COLUMNS else to_int(raw)
            yield out


def main() -> int:
    src = find_source()
    log(f"원본: {src.name} ({src.stat().st_size:,} bytes)")

    tag = os.environ.get("DATA_TAG", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "insomnuss/git_action_test_road")

    manifest = build_outputs_from_rows(
        iter_standard_rows(src),
        DATA_DIR,
        source_label="한국도로공사 영업소간 통행요금 조회(FILE) / 공공데이터포털",
        source_file_label=src.name,
        tag=tag,
        repo=repo,
    )

    log(f"행 수: {manifest['rows']:,}")
    for f in manifest["files"]:
        log(f"{f['name']:22} {f['bytes']:>12,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
