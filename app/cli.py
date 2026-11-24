"""Dòng lệnh: nhập USDA, nhập công thức/giá/nguyên liệu, duyệt, nạp demo, xem trạng thái.

    python -m app.cli import-usda --source ".\\FoodData_Central_sr_legacy_food_csv_2018-04"
    python -m app.cli import-usda --source ".\\FoodData_Central_csv_2026-10-29.zip"
    python -m app.cli import-recipes --recipes data\\recipes.csv --ingredients data\\recipe_ingredients.csv
    python -m app.cli approve --recipe R001 --version 1      (hoặc --batch 5)
    python -m app.cli import-prices --file data\\prices.csv
    python -m app.cli import-ingredients --file data\\ingredients.csv
    python -m app.cli load-demo
    python -m app.cli status
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app.catalog import manage
from app.catalog.health import store_policy
from app.catalog.usda_import import import_usda
from app.config import get_settings
from app.storage.db import connect, get_meta


def _print_batch(rep):
    d = rep.to_dict()
    print(f"Trạng thái: {d['status']} · nhận {len(d['accepted'])} · không đổi {len(d['unchanged'])} · từ chối {len(d['rejected'])}")
    for e in d["errors"][:50]:
        print(f"  LỖI {e.get('file')} dòng {e.get('line')} [{e.get('field')}]: {e.get('message')}")
    for w in d["warnings"][:20]:
        print(f"  cảnh báo: {w}")
    if d.get("batch_id"):
        print(f"Mã lô nhập: {d['batch_id']}")
    return 0 if d["status"] in ("ok", "partial") else 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    ap.add_argument("--db", help="Đường dẫn SQLite (mặc định theo APP_DB_PATH)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-usda")
    p.add_argument("--source", required=True)
    p.add_argument("--release-id")
    p.add_argument("--data-type", default="sr_legacy_food")
    p.add_argument("--skip-invalid-rows", action="store_true")
    p = sub.add_parser("import-recipes")
    p.add_argument("--recipes", required=True)
    p.add_argument("--ingredients", required=True)
    p.add_argument("--dataset", default="operational", choices=manage.DATASETS)
    p = sub.add_parser("approve")
    p.add_argument("--recipe")
    p.add_argument("--version", type=int)
    p.add_argument("--batch", type=int)
    for name in ("import-prices", "import-ingredients"):
        p = sub.add_parser(name)
        p.add_argument("--file", required=True)
        p.add_argument("--dataset", default="operational", choices=manage.DATASETS)
    p = sub.add_parser("import-health-policy", help="Nạp bộ quy tắc theo bệnh đã được nhóm duyệt")
    p.add_argument("--file", required=True)
    sub.add_parser("load-demo")
    sub.add_parser("load-disease-demo", help="Bổ sung 6 công thức DEMO cho kịch bản bệnh")
    sub.add_parser("load-viet-recipes", help="Bổ sung 50 món Việt, không nạp giá/quy cách")
    sub.add_parser("status")
    a = ap.parse_args(argv)

    s = get_settings()
    conn = connect(a.db or s.db_path)
    if a.cmd == 'load-viet-recipes':
        from app.catalog.recipe_expansion import load
        try:
            print(json.dumps(load(conn), ensure_ascii=False))
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if a.cmd == 'load-disease-demo':
        from app.catalog.disease_demo import load
        try:
            print(json.dumps(load(conn),ensure_ascii=False))
            return 0
        except ValueError as exc:
            print(str(exc),file=sys.stderr)
            return 1
    if a.cmd == "import-usda":
        rep = import_usda(conn, a.source, release_id=a.release_id, data_type=a.data_type,
                          skip_invalid_rows=a.skip_invalid_rows, progress=lambda m: print("  …", m, flush=True))
        print("\n".join(rep.summary_lines()))
        s.reports_dir.mkdir(parents=True, exist_ok=True)
        out = s.reports_dir / f"usda_import_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Báo cáo: {out}")
        return 0 if rep.status in ("imported", "replaced", "unchanged") else 1
    if a.cmd == "import-recipes":
        rep = manage.import_recipes(conn, Path(a.recipes).read_text(encoding="utf-8-sig"),
                                    Path(a.ingredients).read_text(encoding="utf-8-sig"), a.dataset)
        code = _print_batch(rep)
        print("Công thức mới ở trạng thái chờ duyệt. Duyệt: python -m app.cli approve --batch", rep.batch_id)
        return code
    if a.cmd == "approve":
        if a.batch:
            for m in manage.approve_batch(conn, a.batch):
                print(m)
            return 0
        ok, msg = manage.approve_recipe(conn, a.recipe, a.version)
        print(msg)
        return 0 if ok else 1
    if a.cmd == "import-prices":
        return _print_batch(manage.import_prices(conn, Path(a.file).read_text(encoding="utf-8-sig"), a.dataset))
    if a.cmd == "import-ingredients":
        return _print_batch(manage.import_ingredient_info(conn, Path(a.file).read_text(encoding="utf-8-sig"), a.dataset))
    if a.cmd == "import-health-policy":
        try:
            data = json.loads(Path(a.file).read_text(encoding="utf-8-sig"))
            created = store_policy(conn, data)
        except (OSError, ValueError) as exc:
            print(f"Lỗi nhập bộ quy tắc: {exc}", file=sys.stderr)
            return 1
        print("Đã nạp bộ quy tắc." if created else "Bộ quy tắc đã có cùng nội dung.")
        return 0
    if a.cmd == "load-demo":
        if not get_meta(conn, "active_usda_release"):
            print("Cần nhập USDA trước (import-usda).")
            return 1
        out = manage.load_demo(conn)
        r = out["recipes"]
        print(f"Đã nạp DEMO: {len(r['accepted'])} công thức mới, {len(r['unchanged'])} không đổi, lỗi {len(r['errors'])}.")
        for e in r["errors"]:
            print("  LỖI", e["message"])
        print(f"Quy tắc giả lập: {out['rules']}. Đặt APP_DATA_MODE=demo để dùng bộ này.")
        return 0 if not r["errors"] else 1
    if a.cmd == "status":
        print("Cấu hình:", s)
        print("USDA đang dùng:", get_meta(conn, "active_usda_release"))
        for r in conn.execute("SELECT dataset, status, COUNT(*) n FROM recipe GROUP BY dataset, status"):
            print(f"  công thức {r['dataset']}/{r['status']}: {r['n']}")
        for r in conn.execute("SELECT dataset, COUNT(*) n FROM price_item GROUP BY dataset"):
            print(f"  giá {r['dataset']}: {r['n']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
