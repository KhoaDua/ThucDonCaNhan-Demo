"""Thông báo đổi món; giữ nguyên kết quả và ngưỡng của bộ kiểm tra."""
from app.catalog.lookup import METRICS
from app.planner.constraints import fmt


def swap_failure_reasons(violations, extra=()):
    reasons, seen = [], set()
    for row in violations:
        if row.get('status') not in ('fail', 'missing'):
            continue
        scope = row.get('scope_label', 'Thực đơn')
        measured, limit, op = row.get('measured'), row.get('limit'), row.get('op')
        kind = row.get('kind')
        text = row.get('detail', 'Chưa đáp ứng điều kiện')
        if kind in ('nutrient', 'nutrient_ratio') and row['status']=='fail' and measured is not None and limit is not None:
            upper = op in ('<', '<=')
            title = METRICS[row['metric']][2] if kind=='nutrient' else row['label'].split('≤')[0].split('≥')[0].strip()
            unit = row.get('unit', '')
            delta = abs(measured-limit)
            difference_unit = 'điểm phần trăm' if unit.startswith('%') else unit
            if delta > 1e-8:
                symbol = {'<':'<','<=':'≤','>':'>','>=':'≥','==':'='}.get(op,op)
                text = (f"{title} {'vượt giới hạn' if upper else 'thiếu so với mức tối thiểu'} {fmt(delta)} {difference_unit}: "
                        f"{fmt(measured)} {unit}; yêu cầu {symbol} {fmt(limit)} {unit}.")
            else:
                text = f"{title} {fmt(measured)} {unit} chưa thỏa điều kiện {op} {fmt(limit)} {unit}."
            key = (kind, title, scope, measured, limit, 'upper' if upper else 'lower', unit)
        else:
            if row['status']=='missing' and 'Chưa phân loại:' in text:
                text = 'Chưa đủ dữ liệu phân loại nguyên liệu: '+text.split('Chưa phân loại:',1)[1].strip()
            key = (scope, text)
        if key not in seen:
            reasons.append(f'{scope}: {text}')
            seen.add(key)
    for message in extra:
        if message not in reasons:
            reasons.append(message)
    return reasons
