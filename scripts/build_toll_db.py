#!/usr/bin/env python3
"""
한국도로공사 "영업소간 통행요금 조회(FILE)" 원본을 앱 배포용으로 변환한다.

원본은 CP949 CSV 83MB(35만행)라 그대로는 앱에 내려보낼 수 없다. 필요한 컬럼만 뽑아
SQLite(인덱스 포함) + gzip CSV로 만들고, 앱이 버전 비교에 쓸 manifest.json을 함께 낸다.

담는 것: 출발/도착 영업소, 운영기관, 1~8종 정상요금, 할인 시간대 2구간(1~8종 + 시작/종료)
빼는 것: 차로별 거리, 주행시간, 경로수 등 요금 계산에 불필요한 컬럼

컬럼은 이름으로 찾는다. 원본 컬럼 순서가 바뀌어도 깨지지 않게 하기 위함이다.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SOURCE_DIR = DATA_DIR / "source"

# 원본 헤더명 → 우리 컬럼명. 원본이 CP949라 한글 그대로 매칭한다.
BASE_COLUMNS = {
    "출발영업소코드": "start_code",
    "출발영업소명": "start_name",
    "도착영업소코드": "end_code",
    "도착영업소명": "end_name",
    "고속도로운영기관코드": "operator_code",
    "고속도로운영기관명": "operator_name",
}
# 1~8종 정상요금
for _i in range(1, 9):
    BASE_COLUMNS[f"정상{_i}종금액"] = f"fare{_i}"
# 할인 시간대 2구간
for _n, _prefix in ((1, "d1"), (2, "d2")):
    for _i in range(1, 9):
        BASE_COLUMNS[f"{_n}번째할인시간대할인{_i}종금액"] = f"{_prefix}_fare{_i}"
    BASE_COLUMNS[f"{_n}번째할인시간대할인시작시분"] = f"{_prefix}_start"
    BASE_COLUMNS[f"{_n}번째할인시간대할인종료시분"] = f"{_prefix}_end"

TEXT_COLUMNS = {"start_code", "start_name", "end_code", "end_name",
                "operator_code", "operator_name", "d1_start", "d1_end", "d2_start", "d2_end"}


def log(msg: str) -> None:
    print(msg, flush=True)


def find_source() -> Path:
    """data/source/ 안의 원본 파일(확장자가 .zip이어도 실제는 gzip)을 찾는다."""
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


def to_int(value: str) -> int:
    v = (value or "").strip()
    if not v:
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    src = find_source()
    log(f"원본: {src.name} ({src.stat().st_size:,} bytes)")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 빌드용 임시 경로 - 완성 후 gzip으로 감싸 커밋용 파일만 남긴다.
    # SQLite 원본은 인덱스 포함 42MB라 그대로 배포하기엔 크고, gzip하면 8MB대로 줄어든다.
    db_tmp_path = DATA_DIR / "_toll_fares.sqlite.tmp"
    db_gz_path = DATA_DIR / "toll_fares.sqlite.gz"
    csv_gz_path = DATA_DIR / "toll_fares.csv.gz"
    if db_tmp_path.exists():
        db_tmp_path.unlink()

    out_columns = list(BASE_COLUMNS.values())

    conn = sqlite3.connect(db_tmp_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    cols_ddl = ", ".join(
        f"{c} {'TEXT' if c in TEXT_COLUMNS else 'INTEGER'}" for c in out_columns
    )
    conn.execute(f"CREATE TABLE toll_fare ({cols_ddl})")

    rows_written = 0
    with open_source(src) as fin, gzip.open(csv_gz_path, "wt", encoding="utf-8", newline="", compresslevel=9) as fgz:
        reader = csv.reader(fin)
        header = next(reader)
        header = [h.strip().strip('"') for h in header]

        missing = [k for k in BASE_COLUMNS if k not in header]
        if missing:
            raise SystemExit(f"원본에 없는 컬럼: {missing[:5]} ... (원본 형식이 바뀌었을 수 있다)")
        idx = {BASE_COLUMNS[name]: header.index(name) for name in BASE_COLUMNS}

        writer = csv.writer(fgz)
        writer.writerow(out_columns)

        placeholders = ",".join("?" * len(out_columns))
        insert_sql = f"INSERT INTO toll_fare VALUES ({placeholders})"
        batch: list[tuple] = []

        for row in reader:
            if not row:
                continue
            values = []
            for col in out_columns:
                raw = row[idx[col]] if idx[col] < len(row) else ""
                values.append(raw.strip() if col in TEXT_COLUMNS else to_int(raw))
            batch.append(tuple(values))
            writer.writerow(values)
            rows_written += 1
            if len(batch) >= 20000:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)

    # 앱은 (출발, 도착) 쌍으로만 조회하므로 복합 인덱스 하나면 충분하다.
    conn.execute("CREATE INDEX idx_od ON toll_fare(start_code, end_code)")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    # gzip으로 감싼 뒤 임시 원본은 지운다 - 커밋에는 압축본만 들어간다.
    with db_tmp_path.open("rb") as fin, gzip.open(db_gz_path, "wb", compresslevel=9) as fout:
        fout.writelines(fin)
    raw_size = db_tmp_path.stat().st_size
    db_tmp_path.unlink()

    now = datetime.now(timezone.utc).astimezone()
    repo = os.environ.get("GITHUB_REPOSITORY", "insomnuss/git_action_test_road")
    tag = os.environ.get("DATA_TAG", "").strip()
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
        "source": "한국도로공사 영업소간 통행요금 조회(FILE) / 공공데이터포털",
        "source_file": src.name,
        "rows": rows_written,
        "columns": out_columns,
        "files": files,
    }
    (DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    log(f"행 수      : {rows_written:,}")
    log(f"SQLite 원본: {raw_size:,} bytes (커밋 대상 아님)")
    for f in files:
        log(f"{f['name']:22} {f['bytes']:>12,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
