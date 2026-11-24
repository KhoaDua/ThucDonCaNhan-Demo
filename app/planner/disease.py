"""Quy tắc bệnh DEMO do chủ hệ thống duyệt; không chứng nhận điều trị lâm sàng."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
POLICY = json.loads((ROOT/'disease_policy.json').read_text(encoding='utf-8'))
FOODS = json.loads((ROOT/'disease_foods.json').read_text(encoding='utf-8'))


def requested(profile):
    health = (profile.get('checklist') or {}).get('health') or {}
    return bool(health.get('conditions')) and health.get('simulation_version') == 1


def health_of(profile):
    return (profile.get('checklist') or {}).get('health') or {}


def body_scope_scenarios(mode):
    """Hai kịch bản v1 đã định nghĩa người trưởng thành ngoài thai kỳ và cho con bú."""
    if mode != 'demo' or POLICY['mode'] != mode or POLICY['version'] != 1:
        return ()
    return tuple(key for key in ('maintenance', 'weight_loss') if key in POLICY['scenarios'])


def uses_body_scope_assumption(health, scenarios):
    return (
        bool(health.get('conditions'))
        and set(health['conditions']) <= {'type2_diabetes', 'gout'}
        and health.get('simulation_version') == 1
        and health.get('simulation_scope_confirmed') is True
        and health.get('simulation_scenario') in scenarios
    )


def fingerprint():
    return hashlib.sha256(json.dumps([POLICY,FOODS],sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def identity(profile):
    cs = health_of(profile).get('conditions',[])
    return POLICY['policies']['combined' if len(cs)==2 else cs[0]]


def resolve_energy(profile, result):
    health = health_of(profile)
    if not health.get('simulation_scope_confirmed'):
        raise ValueError('Xác nhận phạm vi kịch bản mô phỏng theo bệnh trước khi lập thực đơn.')
    cs = health.get('conditions',[])
    if len(cs) != len(set(cs)) or not cs or any(c not in ('type2_diabetes','gout') for c in cs):
        raise ValueError('Kịch bản bệnh không được hỗ trợ.')
    scenario = health.get('simulation_scenario')
    if scenario not in POLICY['scenarios']:
        raise ValueError('Chọn kịch bản giữ cân hoặc giảm cân theo bệnh.')
    if result['bmi'] < POLICY['minimum_bmi']:
        raise ValueError('Hồ sơ thiếu cân nằm ngoài phạm vi bộ quy tắc bệnh mô phỏng này.')
    deficit = 0
    if scenario == 'weight_loss':
        if result['bmi'] < POLICY['weight_loss_bmi']:
            raise ValueError('Kịch bản giảm cân theo bệnh chỉ áp dụng khi BMI từ 25; hãy chọn giữ cân.')
        deficit = POLICY['deficit_kcal']
    maintenance = result['day_target']
    effective = maintenance - deficit
    from app.planner.energy import POLICY as BODY_POLICY
    if deficit and effective*(1-BODY_POLICY['tolerance']) < POLICY['energy_floor'][profile['body']['sex']]:
        raise ValueError('Cận dưới calo sau giảm thấp hơn ngưỡng chặn DEMO 1.200 kcal (nữ) / 1.500 kcal (nam). Kịch bản không phù hợp; hệ thống không tự nới giới hạn.')
    rule = identity(profile)
    return {**result,'maintenance_target':maintenance,'day_target':effective,'deficit_kcal':deficit,
            'energy_reason':POLICY['scenarios'][scenario]+' · '+rule['label'],
            'disease_policy_id':rule['id'],'disease_policy_version':POLICY['version'],
            'disease_policy_fingerprint':fingerprint()}


def build(profile, catalog, mode):
    """Tạo điều kiện trong cùng mô hình tuyến tính đang có. Không ghi CSDL."""
    if not requested(profile):
        return [], None
    if mode != 'demo':
        raise ValueError('Bộ quy tắc bệnh này chỉ được duyệt cho mô phỏng DEMO, chưa áp dụng thực tế.')
    from app.planner.energy import energy_constraints
    _, body = energy_constraints(profile)  # recalculate, never use body_result from caller
    health = health_of(profile)
    selected = sorted(health['conditions'])
    rule = identity(profile)
    common = dict(hard=True,editable=False,category='health_limit',demo_rule=True,condition='+'.join(selected),
                  source=rule['id']+' · Chủ hệ thống duyệt mô phỏng; chưa kiểm định lâm sàng',source_version=POLICY['version'])
    cons=[]
    def ratio(cid, terms, op, label, nutrient, scale, threshold, unit, scope='day'):
        cons.append(dict(common,cid='disease:'+cid,kind='nutrient_ratio',terms=terms,op=op,value=0.0,scope=scope,
                         nutrient=nutrient,display_scale=scale,threshold=threshold,unit=unit,label=label))
    ratio('fiber',{'fiber_g':1,'energy_kcal':-POLICY['fiber_per_1000_kcal']/1000},'>=',
          'Chất xơ ≥ 14 g/1.000 kcal thực tế (các bữa được lập)','fiber_g',1000,14,'g/1.000 kcal')
    ratio('saturated',{'saturated_fat_g':9,'energy_kcal':-POLICY['saturated_fat_energy_max']},'<=',
          'Chất béo bão hòa ≤ 10% năng lượng thực tế','saturated_fat_g',900,10,'% năng lượng')
    cons.append(dict(common,cid='disease:sodium',kind='nutrient',metric='sodium_mg',op='<',
                     value=POLICY['sodium_mg_day']*body['selected_share'],unit='mg',scope='day',
                     label=f"Natri < {POLICY['sodium_mg_day']*body['selected_share']:g} mg/ngày cho các bữa đã chọn"))
    if 'type2_diabetes' in selected:
        lo,hi=POLICY['policies']['type2_diabetes']['carb_range']
        for scope in ('day','meal'):
            for name,op,bound in [('min','>=',lo),('max','<=',hi)]:
                ratio('carb:'+scope+':'+name,{'carb_g':4,'energy_kcal':-bound},op,
                      f"Carbohydrate {('≥' if op=='>=' else '≤')} {bound*100:g}% năng lượng thực tế {'mỗi bữa' if scope=='meal' else 'mỗi ngày'} (tham số DEMO)",
                      'carb_g',400,bound*100,'% năng lượng',scope)
    entries={r['fdc_id']:r for r in FOODS['foods']}
    available={r['fdc_id'] for r in catalog.reviewed_ingredients()}
    available.update(i['fdc_id'] for r in catalog.list_active_recipes() for i in r['ingredients'])
    # Include listed banned IDs even before a recipe uses them; all unlisted IDs remain unknown.
    for condition in selected:
        groups=set(POLICY['policies'][condition]['groups'])
        members,nonmembers=[],[]
        for fid in sorted(available | set(entries)):
            entry=entries.get(fid)
            food=catalog.get_food(fid)
            if not entry or not food or not food['description'].startswith(entry['expected_description_prefix']):
                continue
            if groups.intersection(entry['groups']):
                members.append(fid)
            elif condition not in entry['unknown_for']:
                nonmembers.append(fid)
        names=[entries[f]['name'] for f in members]
        cons.append(dict(common,cid='disease:exclude:'+condition,kind='food_group',name=POLICY['policies'][condition]['label']+' · nguyên liệu loại trừ',
                         members=members,nonmembers=nonmembers,op='<=',value=0,unit='món',scope='period',
                         classification_source='Danh mục mã FDC mô phỏng v'+str(FOODS['version'])+' · '+POLICY['sources']['gout' if condition=='gout' else 'ada_nutrition'],
                         label='Loại hoàn toàn theo '+POLICY['policies'][condition]['label']+': '+(', '.join(names) or 'các nhóm đã cấu hình')))
    result={'policy_id':rule['id'],'policy_version':POLICY['version'],'fingerprint':fingerprint(),
            'conditions':selected,'scenario':health['simulation_scenario'],'approved_by':POLICY['approved_by'],
            'clinical_reviewed':False,'scope':POLICY['scope'],'sources':POLICY['sources'],
            'excluded_foods':sorted({entries[f]['name'] for c in cons if c['kind']=='food_group' for f in c['members']}),
            'energy_target':body['day_target'],'maintenance_target':body['maintenance_target'],'deficit_kcal':body['deficit_kcal']}
    return cons,result


def integrity_errors(profile, constraints, catalog, mode):
    try:
        expected,result=build(profile,catalog,mode)
    except (ValueError,KeyError,TypeError) as exc:
        return [str(exc)]
    actual=[c for c in constraints if str(c.get('cid','')).startswith('disease:')]
    if actual!=expected or profile.get('disease_result')!=result:
        return ['Bộ quy tắc bệnh hoặc danh mục nguyên liệu đã thay đổi. Hãy cập nhật và xác nhận lại kịch bản mô phỏng.']
    return []
