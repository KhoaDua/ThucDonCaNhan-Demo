"""Nhập USDA FoodData Central vào SQLite (FR-04).

Nguồn có thể là thư mục CSV (ví dụ `FoodData_Central_sr_legacy_food_csv_2018-04`) hoặc
tệp zip của bản phát hành đầy đủ (ví dụ `FoodData_Central_csv_2026-10-29.zip`). Chỉ nhập
thực phẩm thuộc `data_type` đã chọn (mặc định `sr_legacy_food`).

Nguyên tắc theo DATA_CONTRACT:
- Ghép `food.fdc_id = food_nutrient.fdc_id` và `food_nutrient.nutrient_id = nutrient.id`.
- `amount` là lượng chất trên 100 g, theo `nutrient.unit_name`.
- Dòng vắng mặt là CHƯA BIẾT; dòng có giá trị 0 là dữ liệu 0 thật.
- Tệp sai cấu trúc không thay thế bản đã nhập hợp lệ (toàn bộ trong một giao dịch).
- Nhập lại cùng bản phát hành không tạo dòng trùng.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from app.storage.db import now_iso, set_meta, transaction

REQUIRED_COLUMNS = {
    "food.csv": ["fdc_id", "data_type", "description", "food_category_id", "publication_date"],
    "food_nutrient.csv": ["id", "fdc_id", "nutrient_id", "amount", "derivation_id"],
    "nutrient.csv": ["id", "name", "unit_name", "nutrient_nbr"],
}
OPTIONAL_COLUMNS = {
    "sr_legacy_food.csv": ["fdc_id", "NDB_number"],
    "food_category.csv": ["id", "code", "description"],
    "food_portion.csv": ["id", "fdc_id", "amount", "measure_unit_id", "modifier", "gram_weight"],
    "measure_unit.csv": ["id", "name"],
}
# Chỉ tiêu theo dõi trong báo cáo nhập (nutrient.id, không dùng nutrient_nbr)
TRACKED_REPORT = {1008: "Năng lượng (kcal)", 1003: "Protein (g)", 1004: "Chất béo tổng (g)",
                  1005: "Carbohydrate sai biệt (g)", 1079: "Chất xơ tổng (g)", 1093: "Natri (mg)",
                  2000: "Đường tổng (g)"}
CORE6 = (1008, 1003, 1004, 1005, 1079, 1093)


class StructureError(Exception):
    """Thiếu tệp hoặc thiếu cột bắt buộc: dừng trước khi chạm vào CSDL."""


class _Unchanged(Exception):
    pass


class _DataErrors(Exception):
    pass


@dataclass
class ImportReport:
    release_id: str
    label: str
    source: str
    data_type: str
    status: str = "pending"
    counts: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    error_count: int = 0
    skipped_rows: int = 0
    warnings: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    foods_with_core6: int = 0
    foods_missing_sodium: int = 0
    portion_units: dict = field(default_factory=dict)
    fingerprint: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    def summary_lines(self) -> list[str]:
        lines = [
            f"Bản phát hành: {self.release_id} ({self.label})",
            f"Nguồn: {self.source} · data_type: {self.data_type}",
            f"Trạng thái: {self.status}",
        ]
        for k, v in self.counts.items():
            lines.append(f"  {k}: {_n(v)}")
        if self.coverage:
            lines.append("Số thực phẩm có dữ liệu theo chỉ tiêu:")
            for k, v in self.coverage.items():
                lines.append(f"  {k}: {_n(v)}")
            lines.append(f"Đủ 6 chỉ tiêu chính: {_n(self.foods_with_core6)}")
            lines.append(f"Thiếu dòng natri (chưa biết, không phải 0): {_n(self.foods_missing_sodium)}")
        if self.portion_units:
            lines.append(f"Đơn vị trong food_portion: {self.portion_units}")
        for w in self.warnings:
            lines.append(f"Cảnh báo: {w}")
        if self.error_count:
            lines.append(f"Lỗi dữ liệu: {self.error_count} (bỏ qua {self.skipped_rows} dòng)")
            for e in self.errors[:20]:
                lines.append(f"  - {e['file']} dòng {e['line']}: {e['message']}")
        lines.append(f"Thời gian: {self.duration_s:.1f} giây")
        return lines


def _n(v) -> str:
    return f"{v:,}".replace(",", ".") if isinstance(v, int) else str(v)


class _Source:
    """Đọc CSV UTF-8 (có BOM) từ thư mục hoặc zip."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.zip = None
        self.prefix = ""
        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self.zip = zipfile.ZipFile(self.path)
            names = self.zip.namelist()
            food = [n for n in names if n.endswith("/food.csv") or n == "food.csv"]
            if not food:
                raise StructureError("Không tìm thấy food.csv trong tệp zip")
            self.prefix = food[0][: -len("food.csv")]
        elif not self.path.is_dir():
            raise StructureError(f"Không tìm thấy thư mục hoặc tệp zip nguồn: {self.path}")

    def exists(self, name: str) -> bool:
        if self.zip:
            try:
                self.zip.getinfo(self.prefix + name)
                return True
            except KeyError:
                return False
        return (self.path / name).exists()

    def open(self, name: str):
        if self.zip:
            return io.TextIOWrapper(self.zip.open(self.prefix + name), encoding="utf-8-sig", newline="")
        return open(self.path / name, encoding="utf-8-sig", newline="")

    def rows(self, name: str, required: list[str]) -> Iterator[tuple[int, dict]]:
        with self.open(name) as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise StructureError(f"{name} rỗng")
            header = [h.strip() for h in header]
            missing = [c for c in required if c not in header]
            if missing:
                raise StructureError(f"{name} thiếu cột: {', '.join(missing)}")
            idx = {c: header.index(c) for c in header}
            for line_no, row in enumerate(reader, start=2):
                if len(row) < len(header):
                    yield line_no, None  # dòng sai cấu trúc
                    continue
                yield line_no, {c: row[i] for c, i in idx.items()}

    def check_header(self, name: str, required: list[str]) -> None:
        for _ in self.rows(name, required):
            break


def _int(v: str):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        f = float(v)
    except ValueError:
        raise ValueError(f"không phải số nguyên: {v!r}")
    if f != int(f):
        raise ValueError(f"không phải số nguyên: {v!r}")
    return int(f)


def _float(v: str):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"không phải số: {v!r}")


def default_release_id(source: Path, data_type: str) -> tuple[str, str]:
    name = Path(source).name
    stem = re.sub(r"\.zip$", "", name, flags=re.I)
    if "sr_legacy_food_csv_2018-04" in stem:
        return "SR_LEGACY_2018_04", "USDA FoodData Central SR Legacy 04/2018"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
    tag = m.group(1) if m else stem
    label = f"USDA FoodData Central {tag} – lọc {data_type}"
    if data_type == "sr_legacy_food":
        label += " (dữ liệu SR Legacy 04/2018 phát hành lại)"
    return f"FDC_{tag}_{data_type}", label


def import_usda(conn, source: Path | str, *, release_id: str | None = None, label: str | None = None,
                data_type: str = "sr_legacy_food", skip_invalid_rows: bool = False,
                max_errors: int = 200, progress=None) -> ImportReport:
    t0 = time.time()
    source = Path(source)
    rid, rlabel = default_release_id(source, data_type)
    rep = ImportReport(release_id=release_id or rid, label=label or rlabel, source=str(source), data_type=data_type)
    errors: list[dict] = []
    err_total = 0

    def add_error(file, line, msg):
        nonlocal err_total
        err_total += 1
        if len(errors) < max_errors:
            errors.append({"file": file, "line": line, "message": msg})

    try:
        src = _Source(source)
        for name, cols in REQUIRED_COLUMNS.items():
            if not src.exists(name):
                raise StructureError(f"Thiếu tệp bắt buộc {name}")
            src.check_header(name, cols)
        optional = {}
        for name, cols in OPTIONAL_COLUMNS.items():
            if src.exists(name):
                src.check_header(name, cols)
                optional[name] = cols
            else:
                rep.warnings.append(f"Không có {name}; bỏ qua phần tương ứng")
    except StructureError as exc:
        rep.status = "failed"
        rep.errors = [{"file": "-", "line": 0, "message": str(exc)}]
        rep.error_count = 1
        rep.duration_s = time.time() - t0
        _log(conn, rep)
        return rep

    fp = hashlib.sha256()
    existing = conn.execute("SELECT fingerprint FROM usda_release WHERE release_id=?", (rep.release_id,)).fetchone()
    R = rep.release_id
    try:
        with transaction(conn):
            for t in ("usda_food", "usda_nutrient", "usda_food_nutrient", "usda_food_category",
                      "usda_food_portion", "usda_measure_unit"):
                conn.execute(f"DELETE FROM {t} WHERE release_id=?", (R,))

            # nutrient.csv
            nutrients: dict[int, tuple] = {}
            for line, row in src.rows("nutrient.csv", REQUIRED_COLUMNS["nutrient.csv"]):
                if row is None:
                    add_error("nutrient.csv", line, "dòng thiếu cột")
                    continue
                try:
                    nid = _int(row["id"])
                    if nid is None:
                        raise ValueError("id trống")
                    if nid in nutrients:
                        raise ValueError(f"id trùng {nid}")
                    unit = (row["unit_name"] or "").strip().lower()
                    if not unit:
                        raise ValueError("unit_name trống")
                    nutrients[nid] = (R, nid, row["name"].strip(), unit, (row["nutrient_nbr"] or "").strip())
                except ValueError as e:
                    add_error("nutrient.csv", line, str(e))
            conn.executemany("INSERT INTO usda_nutrient VALUES(?,?,?,?,?)", list(nutrients.values()))
            for v in sorted(nutrients.values()):
                fp.update(repr(v[1:]).encode())

            # sr_legacy_food.csv (mã NDB cũ) và tập fdc_id thuộc data_type
            ndb: dict[int, str] = {}
            if "sr_legacy_food.csv" in optional:
                for line, row in src.rows("sr_legacy_food.csv", optional["sr_legacy_food.csv"]):
                    if row is None:
                        continue
                    try:
                        fid = _int(row["fdc_id"])
                        if fid is not None:
                            ndb[fid] = (row.get("NDB_number") or "").strip()
                    except ValueError as e:
                        add_error("sr_legacy_food.csv", line, str(e))

            # food.csv
            foods: dict[int, tuple] = {}
            other_types = 0
            for line, row in src.rows("food.csv", REQUIRED_COLUMNS["food.csv"]):
                if row is None:
                    add_error("food.csv", line, "dòng thiếu cột")
                    continue
                if (row["data_type"] or "").strip() != data_type:
                    other_types += 1
                    continue
                try:
                    fid = _int(row["fdc_id"])
                    if fid is None or fid <= 0:
                        raise ValueError("fdc_id trống hoặc không hợp lệ")
                    if fid in foods:
                        raise ValueError(f"fdc_id trùng {fid}")
                    desc = (row["description"] or "").strip()
                    if not desc:
                        raise ValueError("description trống")
                    try:
                        cat = _int(row["food_category_id"])
                    except ValueError:
                        cat = None
                    foods[fid] = (R, fid, desc, data_type, cat, (row["publication_date"] or "").strip(), ndb.get(fid, ""))
                except ValueError as e:
                    add_error("food.csv", line, str(e))
            if other_types:
                rep.warnings.append(f"Bỏ qua {other_types:,} thực phẩm thuộc data_type khác {data_type}".replace(",", "."))
            if not foods:
                raise StructureError(f"Không có thực phẩm nào thuộc data_type {data_type}")
            conn.executemany("INSERT INTO usda_food VALUES(?,?,?,?,?,?,?)", list(foods.values()))
            for v in sorted(foods.values(), key=lambda x: x[1]):
                fp.update(repr(v[1:6]).encode())
            if progress:
                progress(f"food.csv: {len(foods)} thực phẩm")

            # food_nutrient.csv (lớn: đọc dạng luồng, chèn theo lô)
            seen: set[int] = set()
            batch: list[tuple] = []
            n_rows = 0
            have: dict[int, set] = {}
            for line, row in src.rows("food_nutrient.csv", REQUIRED_COLUMNS["food_nutrient.csv"]):
                if row is None:
                    add_error("food_nutrient.csv", line, "dòng thiếu cột")
                    rep.skipped_rows += 1
                    continue
                raw_fid = (row["fdc_id"] or "").strip().strip('"')
                try:
                    fid = _int(raw_fid)
                except ValueError as e:
                    add_error("food_nutrient.csv", line, str(e))
                    rep.skipped_rows += 1
                    continue
                if fid not in foods:
                    # thuộc data_type khác (bản đầy đủ) hoặc tham chiếu mồ côi
                    if other_types == 0 or fid in ndb:
                        add_error("food_nutrient.csv", line, f"fdc_id {fid} không có trong food.csv")
                        rep.skipped_rows += 1
                    continue
                try:
                    rid_ = _int(row["id"])
                    nid = _int(row["nutrient_id"])
                    if nid not in nutrients:
                        raise ValueError(f"nutrient_id {nid} không có trong nutrient.csv")
                    amount = _float(row["amount"])
                    if amount is None:
                        raise ValueError("amount trống (không được coi là 0)")
                    if amount < 0:
                        raise ValueError(f"amount âm {amount}")
                    key = fid * 100000 + nid
                    if key in seen:
                        raise ValueError(f"cặp (fdc_id={fid}, nutrient_id={nid}) bị trùng")
                    try:
                        der = _int(row.get("derivation_id", ""))
                    except ValueError:
                        der = None
                except ValueError as e:
                    add_error("food_nutrient.csv", line, str(e))
                    rep.skipped_rows += 1
                    continue
                seen.add(key)
                batch.append((R, rid_, fid, nid, amount, der))
                fp.update(f"{fid}|{nid}|{amount!r}".encode())
                if nid in TRACKED_REPORT:
                    have.setdefault(nid, set()).add(fid)
                n_rows += 1
                if len(batch) >= 5000:
                    conn.executemany("INSERT INTO usda_food_nutrient VALUES(?,?,?,?,?,?)", batch)
                    batch.clear()
                    if progress and n_rows % 100000 == 0:
                        progress(f"food_nutrient.csv: {n_rows} dòng")
            if batch:
                conn.executemany("INSERT INTO usda_food_nutrient VALUES(?,?,?,?,?,?)", batch)

            counts = {"foods": len(foods), "nutrients": len(nutrients), "food_nutrient_rows": n_rows}

            if "food_category.csv" in optional:
                rows = []
                for line, row in src.rows("food_category.csv", optional["food_category.csv"]):
                    if row is None:
                        continue
                    try:
                        rows.append((R, _int(row["id"]), row["code"].strip(), row["description"].strip()))
                    except ValueError as e:
                        add_error("food_category.csv", line, str(e))
                conn.executemany("INSERT OR REPLACE INTO usda_food_category VALUES(?,?,?,?)", rows)
                counts["food_categories"] = len(rows)
            if "measure_unit.csv" in optional:
                rows = []
                for line, row in src.rows("measure_unit.csv", optional["measure_unit.csv"]):
                    if row is None:
                        continue
                    try:
                        rows.append((R, _int(row["id"]), row["name"].strip()))
                    except ValueError as e:
                        add_error("measure_unit.csv", line, str(e))
                conn.executemany("INSERT OR REPLACE INTO usda_measure_unit VALUES(?,?,?)", rows)
                counts["measure_units"] = len(rows)
            if "food_portion.csv" in optional:
                rows = []
                units: dict = {}
                for line, row in src.rows("food_portion.csv", optional["food_portion.csv"]):
                    if row is None:
                        continue
                    try:
                        fid = _int(row["fdc_id"])
                        if fid not in foods:
                            continue
                        mu = _int(row["measure_unit_id"])
                        units[mu] = units.get(mu, 0) + 1
                        rows.append((R, _int(row["id"]), fid, _float(row["amount"]), mu,
                                     (row.get("modifier") or "").strip(), _float(row["gram_weight"])))
                    except ValueError as e:
                        add_error("food_portion.csv", line, str(e))
                conn.executemany("INSERT OR REPLACE INTO usda_food_portion VALUES(?,?,?,?,?,?,?)", rows)
                counts["food_portions"] = len(rows)
                rep.portion_units = {str(k): v for k, v in units.items()}
            rep.counts = counts

            rep.coverage = {TRACKED_REPORT[n]: len(have.get(n, ())) for n in TRACKED_REPORT}
            core = None
            for n in CORE6:
                s = have.get(n, set())
                core = set(s) if core is None else core & s
            rep.foods_with_core6 = len(core or ())
            rep.foods_missing_sodium = len(foods) - len(have.get(1093, ()))
            rep.error_count = err_total
            rep.errors = errors
            rep.fingerprint = fp.hexdigest()

            if err_total and not skip_invalid_rows:
                raise _DataErrors()
            if existing and existing["fingerprint"] == rep.fingerprint:
                raise _Unchanged()

            rep.status = "replaced" if existing else "imported"
            rep.duration_s = time.time() - t0
            conn.execute(
                "INSERT OR REPLACE INTO usda_release VALUES(?,?,?,?,?,?,?,?,?)",
                (R, rep.label, str(source), data_type, rep.fingerprint, len(foods), n_rows, now_iso(),
                 json.dumps(rep.to_dict(), ensure_ascii=False)),
            )
            set_meta(conn, "active_usda_release", R)
    except _Unchanged:
        rep.status = "unchanged"
    except _DataErrors:
        rep.status = "failed"
        rep.warnings.append("Có lỗi dữ liệu: không thay đổi bản đã nhập. Dùng --skip-invalid-rows để bỏ qua dòng lỗi.")
    except StructureError as exc:
        rep.status = "failed"
        rep.errors = [{"file": "-", "line": 0, "message": str(exc)}] + errors
        rep.error_count = err_total + 1
    rep.duration_s = time.time() - t0
    _log(conn, rep)
    return rep


def _log(conn, rep: ImportReport) -> None:
    conn.execute(
        "INSERT INTO import_log(kind, dataset, created_at, status, report_json) VALUES(?,?,?,?,?)",
        ("usda", rep.release_id, now_iso(), rep.status, json.dumps(rep.to_dict(), ensure_ascii=False)),
    )
