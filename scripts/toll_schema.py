"""
build_toll_db.py(파일 방식)와 update_toll.py(OpenAPI 방식) 두 파이프라인이
정확히 같은 컬럼 구성의 sqlite.gz/csv.gz/manifest.json을 만들도록 공유하는 모듈이다.
앱은 어느 파이프라인이 만들었는지 몰라도 되는 게 목적이다.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

STANDARD_COLUMNS = (
    ["start_code", "start_name", "end_code", "end_name", "operator_code", "operator_name"]
    + [f"fare{i}" for i in range(1, 9)]
    + [f"d1_fare{i}" for i in range(1, 9)] + ["d1_start", "d1_end"]
    + [f"d2_fare{i}" for i in range(1, 9)] + ["d2_start", "d2_end"]
)

TEXT_COLUMNS = {
    "start_code", "start_name", "end_code", "end_name", "operator_code", "operator_name",
    "d1_start", "d1_end", "d2_start", "d2_end",
}

# 표준 컬럼 -> 원본 응답에서 있을 법한 필드명 후보(우선순위 순).
#
# 1순위(각 리스트의 첫 값)는 data.ex.co.kr OpenOASIS 포털의 실제 API 상세 페이지
# (apiId=0620, 요청 URL data.ex.co.kr/openapi/toll/bhoinstIntoTollList)에서 확인하고
# 실제 서비스키로 호출까지 해서 검증한 진짜 필드명이다. 응답의 count가 355,664로
# FILE 데이터셋과 완전히 일치하는 것도 확인했다 - 같은 데이터, 다른 배포 경로.
#
# 나머지 후보(2번째 이후)는 혹시 필드명이 버전업 등으로 바뀔 경우를 대비한 예비 추측이다.
# 한글 이름은 FILE 데이터셋(data.go.kr/data/15117921) 헤더에서 확인된 값이다.
FIELD_ALIASES: dict[str, list[str]] = {
    "start_code": ["dprtrTolofCd", "출발영업소코드", "startUnitCode", "startTcsCode"],
    "start_name": ["dprtrTolofNm", "출발영업소명", "startUnitName", "startTcsName"],
    "end_code": ["arrvTolofCd", "도착영업소코드", "endUnitCode", "endTcsCode"],
    "end_name": ["arrvTolofNm", "도착영업소명", "endUnitName", "endTcsName"],
    "operator_code": ["hoinstCd", "고속도로운영기관코드", "opCode", "corpCode"],
    "operator_name": ["hoinstNm", "고속도로운영기관명", "opName", "corpName"],
    **{f"fare{i}": [f"nrmlKnd{i}Amt", f"정상{i}종금액", f"tolFare{i}"] for i in range(1, 9)},
    **{f"d1_fare{i}": [f"odn1DcKnd{i}Amt", f"1번째할인시간대할인{i}종금액", f"dcFare1_{i}"]
       for i in range(1, 9)},
    "d1_start": ["odn1DcStrtHhmm", "1번째할인시간대할인시작시분", "dcStart1"],
    "d1_end": ["odn1DcEndHhmm", "1번째할인시간대할인종료시분", "dcEnd1"],
    **{f"d2_fare{i}": [f"odn2DcKnd{i}Amt", f"2번째할인시간대할인{i}종금액", f"dcFare2_{i}"]
       for i in range(1, 9)},
    "d2_start": ["odn2DcStrtHhmm", "2번째할인시간대할인시작시분", "dcStart2"],
    "d2_end": ["odn2DcEndHhmm", "2번째할인시간대할인종료시분", "dcEnd2"],
}

# 이 셋이 매칭 안 되면 데이터로서 쓸모가 없다고 보고 즉시 중단한다.
REQUIRED_COLUMNS = ["start_code", "end_code", "fare1"]


def to_int(value) -> int:
    v = str(value if value is not None else "").strip()
    if not v:
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def normalize_row(raw: dict) -> tuple[dict, int]:
    """원본 딕셔너리 1행을 표준 컬럼으로 매핑한다. (표준행, 매칭된 컬럼 수)를 돌려준다."""
    out: dict[str, object] = {}
    matched = 0
    for col in STANDARD_COLUMNS:
        val = None
        for alias in FIELD_ALIASES[col]:
            # 키 "존재 여부"로 매칭을 센다(값이 null이어도 매칭으로 친다) - 통행료 데이터는
            # 할인 시간대가 없는 구간 등 정당한 null이 흔해서, 값으로 매칭을 판단하면
            # 스키마 자체는 맞는데도 "매칭 부족"으로 오판해 정상 데이터를 실패시킨다.
            # (실측: 출발=도착인 행에서 odn1Dc*/odn2Dc* 필드가 전부 null로 왔다.)
            if alias in raw:
                val = raw[alias]
                matched += 1
                break
        out[col] = (str(val).strip() if val is not None else "") if col in TEXT_COLUMNS else to_int(val)
    return out, matched


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_outputs_from_rows(
    rows: Iterable[dict],
    data_dir: Path,
    *,
    source_label: str,
    source_file_label: str,
    tag: str = "",
    repo: str = "insomnuss/git_action_test_road",
) -> dict:
    """
    표준 컬럼 딕셔너리를 순서대로 흘려받아 toll_fares.sqlite.gz / toll_fares.csv.gz /
    manifest.json을 만든다. rows는 제너레이터여도 된다(35만 행을 한 번에 메모리에
    안 올리려고 스트리밍으로 쓴다).

    row_count가 0이면 아무 파일도 만들지 않고 예외를 던진다 - 실패를 명확히 알려야
    기존 파일을 잘못 덮어쓰는 사고를 막을 수 있다.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    db_tmp_path = data_dir / "_toll_fares.sqlite.tmp"
    db_gz_path = data_dir / "toll_fares.sqlite.gz"
    csv_gz_path = data_dir / "toll_fares.csv.gz"
    if db_tmp_path.exists():
        db_tmp_path.unlink()

    conn = sqlite3.connect(db_tmp_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    cols_ddl = ", ".join(f"{c} {'TEXT' if c in TEXT_COLUMNS else 'INTEGER'}" for c in STANDARD_COLUMNS)
    conn.execute(f"CREATE TABLE toll_fare ({cols_ddl})")
    placeholders = ",".join("?" * len(STANDARD_COLUMNS))
    insert_sql = f"INSERT INTO toll_fare VALUES ({placeholders})"

    row_count = 0
    try:
        # gzip.open은 압축 시각을 헤더에 새겨서, 데이터가 완전히 같아도 매 실행마다
        # 파일 바이트가(따라서 sha256이) 달라진다. 그러면 워크플로의 "데이터가 그대로면
        # 커밋하지 않는다" 체크(git diff)가 무의미해지므로 mtime=0으로 고정해 결정론적으로
        # 만든다 - 같은 입력이면 항상 같은 바이트가 나온다.
        with gzip.GzipFile(csv_gz_path, "wb", compresslevel=9, mtime=0) as gz_raw, \
             io.TextIOWrapper(gz_raw, encoding="utf-8", newline="") as fgz:
            writer = csv.writer(fgz)
            writer.writerow(STANDARD_COLUMNS)
            batch: list[tuple] = []
            for row in rows:
                values = tuple(row[c] for c in STANDARD_COLUMNS)
                writer.writerow(values)
                batch.append(values)
                row_count += 1
                if len(batch) >= 20000:
                    conn.executemany(insert_sql, batch)
                    batch.clear()
            if batch:
                conn.executemany(insert_sql, batch)

        if row_count == 0:
            raise RuntimeError("표준화된 행이 0건이다 - 기존 파일을 보존하고 중단한다")

        conn.execute("CREATE INDEX idx_od ON toll_fare(start_code, end_code)")
        conn.commit()
        conn.execute("VACUUM")
        conn.close()

        with db_tmp_path.open("rb") as fin, \
             gzip.GzipFile(db_gz_path, "wb", compresslevel=9, mtime=0) as fout:
            fout.writelines(fin)
        raw_size = db_tmp_path.stat().st_size
    except Exception:
        conn.close()
        csv_gz_path.unlink(missing_ok=True)
        db_gz_path.unlink(missing_ok=True)
        raise
    finally:
        db_tmp_path.unlink(missing_ok=True)

    now = datetime.now(timezone.utc).astimezone()
    ref = tag if tag else "main"
    cdn = f"https://cdn.jsdelivr.net/gh/{repo}@{ref}"

    files = []
    for path, note, extra in (
        (db_gz_path, "gzip SQLite (인덱스 포함) - 앱은 이걸 받아 압축 해제 후 바로 조회",
         {"uncompressed_bytes": raw_size}),
        (csv_gz_path, "gzip CSV - 다른 도구/디버깅용", {}),
    ):
        files.append({
            "name": path.name,
            "note": note,
            "bytes": path.stat().st_size,
            "sha256": sha256_of(path),
            "url": f"{cdn}/data/{path.name}",
            **extra,
        })

    manifest = {
        "version": tag or now.strftime("%Y%m%d-%H%M"),
        "generated_at": now.isoformat(timespec="seconds"),
        "source": source_label,
        "source_file": source_file_label,
        "rows": row_count,
        "columns": STANDARD_COLUMNS,
        "files": files,
    }
    import json
    (data_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
