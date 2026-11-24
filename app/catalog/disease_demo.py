"""Công thức bổ sung DEMO; định lượng thiết kế để kiểm thử, không phải thực đơn điều trị."""
import csv
import io
from app.catalog.demo import DEMO_INGREDIENTS

VERSION = 2
RECIPES = [
 ('H01','Yến mạch sữa chua chuối đậu phộng (mẫu)',1,'breakfast',10,[(169705,50),(170886,150),(172430,45),(173944,50)]),
 ('H02','Cháo yến mạch đậu phụ bông cải (mẫu)',1,'breakfast;lunch;dinner',20,[(169705,65),(172475,100),(170379,100),(171411,5),(173468,.5)]),
 ('H03','Cơm đậu phụ bông cải cà rốt (mẫu)',1,'lunch;dinner',25,[(168878,150),(172475,150),(170379,200),(170393,80),(171411,12),(173468,.7)]),
 ('H04','Cơm gà rau củ đậu phộng (mẫu)',1,'lunch;dinner',25,[(168878,160),(171077,100),(170379,200),(170393,80),(172430,15),(171411,12),(173468,.7)]),
 ('H05','Cơm cá rô phi rau củ (mẫu)',1,'lunch;dinner',25,[(168878,160),(175176,100),(170379,200),(170393,80),(172430,15),(171411,17),(173468,.7)]),
 ('H06','Cơm đậu phụ cải bó xôi cà rốt (mẫu)',1,'lunch;dinner',20,[(168878,150),(172475,150),(168462,200),(170393,80),(171411,12),(173468,.7)]),
]


def load(conn):
    from app.catalog import manage
    out,lines=io.StringIO(),io.StringIO()
    w,cw=csv.writer(out),csv.writer(lines)
    w.writerow(manage.RECIPE_COLS)
    cw.writerow(['recipe_id','line_no','fdc_id','quantity_g_edible','food_state','conversion_source'])
    for rid,name,base,meals,prep,ingredients in RECIPES:
        w.writerow([rid,name,base,meals,prep,'DEMO — công thức bổ sung cho kịch bản bệnh; chủ hệ thống cho phép thử nghiệm','demo',VERSION])
        for i,(fid,g) in enumerate(ingredients,1):
            cw.writerow([rid,i,fid,g,DEMO_INGREDIENTS[fid][3],'Gam phần ăn được của trạng thái USDA ghi trên dòng; DEMO'])
    rep=manage.import_recipes(conn,out.getvalue(),lines.getvalue(),'demo',expected_desc={f:x[3] for f,x in DEMO_INGREDIENTS.items()})
    if rep.errors:
        raise ValueError(str(rep.errors))
    for rid,*_ in RECIPES:
        ok,msg=manage.approve_recipe(conn,rid,VERSION,'Chủ hệ thống duyệt dùng DEMO; không kiểm định lâm sàng')
        if not ok:
            raise ValueError(msg)
    return rep.to_dict()
