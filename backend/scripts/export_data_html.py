"""导出数据库当前数据为可视化页面（自包含 HTML，双击即开）。

与 schema-viewer 的区别：schema-viewer 看「表长什么样」，本页看「表里有什么」。
流程图直接读取 wf_nodes 的 x/y 坐标渲染，因此与前端设计器的布局完全一致。

用法：
    python scripts/export_data_html.py
产物：docs/data-viewer.html
"""
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models.ecommerce import Order, OrderItem, Product, Sku, User  # noqa: E402
from app.models.workflow import (  # noqa: E402
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowNode,
    WorkflowTransition,
    WorkflowTransitionLog,
)


def collect() -> dict:
    db = SessionLocal()
    try:
        definition = db.execute(
            select(WorkflowDefinition).where(WorkflowDefinition.code == "order_flow")
        ).scalars().first()

        nodes, transitions = [], []
        if definition:
            nodes = [
                {"key": n.key, "name": n.name, "type": n.node_type, "x": n.x, "y": n.y}
                for n in db.execute(
                    select(WorkflowNode).where(WorkflowNode.definition_id == definition.id)
                ).scalars().all()
            ]
            transitions = [
                {"from": t.from_node_key, "to": t.to_node_key, "event": t.event,
                 "condition": t.condition_expr, "desc": t.description}
                for t in db.execute(
                    select(WorkflowTransition).where(
                        WorkflowTransition.definition_id == definition.id
                    )
                ).scalars().all()
            ]

        # 每个节点上停留的订单数，用于在流程图标注负载
        load: dict[str, int] = {}
        for inst in db.execute(select(WorkflowInstance)).scalars().all():
            if inst.status == "running":
                load[inst.current_node_key] = load.get(inst.current_node_key, 0) + 1

        orders = []
        for o in db.execute(select(Order).order_by(Order.id)).scalars().all():
            items = db.execute(
                select(OrderItem).where(OrderItem.order_id == o.id)
            ).scalars().all()
            logs = []
            if o.workflow_instance_id:
                logs = [
                    {"from": lg.from_node_key, "to": lg.to_node_key, "event": lg.event,
                     "operator": lg.operator, "comment": lg.comment,
                     "at": lg.created_at.strftime("%m-%d %H:%M:%S") if lg.created_at else ""}
                    for lg in db.execute(
                        select(WorkflowTransitionLog)
                        .where(WorkflowTransitionLog.instance_id == o.workflow_instance_id)
                        .order_by(WorkflowTransitionLog.id)
                    ).scalars().all()
                ]
            user = db.get(User, o.user_id)
            orders.append({
                "no": o.order_no, "amount": float(o.pay_amount), "status": o.status,
                "user": user.nickname if user else "-", "items": [
                    {"name": i.sku_name, "spec": i.spec, "qty": i.quantity,
                     "price": float(i.price)} for i in items],
                "address": o.address_snapshot, "logs": logs,
            })

        skus = []
        for s in db.execute(select(Sku)).scalars().all():
            p = db.get(Product, s.product_id)
            skus.append({"code": s.sku_code, "product": p.name if p else "-",
                         "spec": s.spec, "price": float(s.price), "stock": s.stock})

        return {"nodes": nodes, "transitions": transitions, "load": load,
                "orders": orders, "skus": skus}
    finally:
        db.close()


HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>flowmart 数据浏览器</title>
<style>
:root{--bg:#0f1419;--panel:#161b22;--panel2:#1c2230;--bd:#2d3542;--tx:#e6edf3;
--mut:#8b949e;--ac:#58a6ff;--ok:#3fb950;--warn:#d29922;--err:#f85149;--gray:#6e7681}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px}
header{padding:18px 26px;border-bottom:1px solid var(--bd);display:flex;gap:22px;align-items:center;flex-wrap:wrap}
h1{margin:0;font-size:18px}.kpi{color:var(--mut);font-size:13px}
.kpi b{color:var(--ac);font-size:15px}
.tabs{padding:10px 26px;display:flex;gap:8px;border-bottom:1px solid var(--bd)}
.tab{background:transparent;border:1px solid var(--bd);color:var(--mut);padding:6px 15px;
border-radius:6px;cursor:pointer;font-size:13px}
.tab.on{background:var(--ac);color:#0d1117;border-color:var(--ac);font-weight:600}
main{padding:20px 26px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
th{background:var(--panel2);text-align:left;padding:9px 12px;font-size:12px;color:var(--mut);font-weight:600}
td{padding:9px 12px;border-top:1px solid #21262d;font-size:13px}
tr:hover td{background:#1a2029}
.b{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11.5px;font-weight:600}
.s-completed{background:rgba(63,185,80,.18);color:var(--ok)}
.s-paid,.s-shipped{background:rgba(88,166,255,.18);color:var(--ac)}
.s-pending_payment{background:rgba(210,153,34,.18);color:var(--warn)}
.s-refunding{background:rgba(210,153,34,.25);color:#f0883e}
.s-closed{background:rgba(110,118,129,.2);color:var(--gray)}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;margin-bottom:12px}
.card h4{margin:0 0 8px;font-size:14px;display:flex;justify-content:space-between}
.mut{color:var(--mut);font-size:12px;font-weight:400}
.tl{list-style:none;margin:8px 0 0;padding:0 0 0 16px;border-left:2px solid var(--bd)}
.tl li{position:relative;padding:4px 0 4px 12px;font-size:12.5px;color:var(--mut)}
.tl li:before{content:'';position:absolute;left:-21px;top:11px;width:8px;height:8px;
border-radius:50%;background:var(--ac)}
.tl b{color:var(--tx)}
#flow{background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:8px;overflow:auto}
.n{fill:var(--panel2);stroke:var(--bd)}.n.start{fill:#12261a;stroke:var(--ok)}
.n.end{fill:#2a1a1a;stroke:var(--err)}.n.hot{stroke:var(--ac);stroke-width:2}
.nt{fill:var(--tx);font-size:12px;font-weight:600}.ns{fill:var(--mut);font-size:10.5px}
.e{stroke:#3d4552;fill:none;stroke-width:1.4}.el{fill:var(--mut);font-size:9.5px}
.badge2{fill:var(--ac);font-size:10px;font-weight:700}
.hint{padding:12px 26px;color:var(--mut);font-size:12px}
</style></head><body>
<header><h1>flowmart 数据浏览器</h1>
<span class="kpi">订单 <b id="k1">0</b> · 流转日志 <b id="k2">0</b> · SKU <b id="k3">0</b></span>
<span class="kpi">由 export_data_html.py 生成</span></header>
<div class="tabs"><button class="tab on" data-v="orders">订单</button>
<button class="tab" data-v="flow">流程图</button>
<button class="tab" data-v="skus">商品</button></div>
<main>
<div id="orders"><table id="ot"></table></div>
<div id="flow" style="display:none"><svg id="fs"></svg></div>
<div id="skus" style="display:none"><table id="st"></table></div>
</main>
<div class="hint">重新生成：python backend/scripts/export_data_html.py</div>
<script>
const D=__DATA__;
const NB={'pending_payment':'待付款',paid:'待发货',shipped:'已发货',completed:'已完成',
closed:'已关闭',refunding:'退款审核中',start:'开始'};
document.getElementById('k1').textContent=D.orders.length;
document.getElementById('k2').textContent=D.orders.reduce((s,o)=>s+o.logs.length,0);
document.getElementById('k3').textContent=D.skus.length;
let h='<tr><th>订单号</th><th>用户</th><th>金额</th><th>状态</th><th>商品</th><th>流转步骤</th></tr>';
D.orders.forEach(o=>{h+=`<tr><td class="mut">${o.no}</td><td>${o.user}</td>
<td>¥${o.amount.toFixed(2)}</td><td><span class="b s-${o.status}">${NB[o.status]||o.status}</span></td>
<td>${o.items.map(i=>i.name+'×'+i.qty).join('，')}</td><td>${o.logs.length} 步</td></tr>`;});
document.getElementById('ot').innerHTML=h;
let s='<tr><th>SKU</th><th>商品</th><th>规格</th><th>价格</th><th>库存</th></tr>';
D.skus.forEach(k=>{s+=`<tr><td class="mut">${k.code}</td><td>${k.product}</td><td>${k.spec}</td>
<td>¥${k.price.toFixed(2)}</td><td>${k.stock}</td></tr>`;});
document.getElementById('st').innerHTML=s;
/* 流程图：直接用 wf_nodes 的 x/y，与设计器布局一致 */
const W=140,H=46;let g='',minX=1e9,minY=1e9,maxX=-1e9,maxY=-1e9;
D.nodes.forEach(n=>{minX=Math.min(minX,n.x-W/2);maxX=Math.max(maxX,n.x+W/2);
minY=Math.min(minY,n.y-H/2);maxY=Math.max(maxY,n.y+H/2);});
const OX=30-minX,OY=30-minY;
const pos={};D.nodes.forEach(n=>pos[n.key]={x:n.x+OX,y:n.y+OY,t:n.type});
D.transitions.forEach(t=>{const a=pos[t.from],b=pos[t.to];if(!a||!b)return;
let x1,y1,x2,y2,c1,c2;
if(b.x>a.x){x1=a.x+W/2;y1=a.y;x2=b.x-W/2;y2=b.y;c1=x1+45;c2=x2-45;}
else{x1=a.x-W/2;y1=a.y;x2=b.x+W/2;y2=b.y;c1=x1-45;c2=x2+45;}
g+=`<path class="e" d="M${x1},${y1} C${c1},${y1} ${c2},${y2} ${x2},${y2}"/>`;
const mx=(x1+x2)/2,my=(y1+y2)/2-4;
g+=`<text class="el" x="${mx}" y="${my}" text-anchor="middle">${t.event}${t.condition?' ('+t.condition+')':''}</text>`;});
D.nodes.forEach(n=>{const p=pos[n.key];const hot=(D.load[n.key]>0);
const cls='n'+(n.type==='start'?' start':n.type==='end'?' end':'')+(hot?' hot':'');
g+=`<g><rect class="${cls}" x="${p.x-W/2}" y="${p.y-H/2}" width="${W}" height="${H}" rx="7"/>
<text class="nt" x="${p.x}" y="${p.y-2}" text-anchor="middle">${n.name}</text>
<text class="ns" x="${p.x}" y="${p.y+14}" text-anchor="middle">${n.key}</text>`;
if(hot)g+=`<circle cx="${p.x+W/2-12}" cy="${p.y-H/2+10}" r="8" fill="#58a6ff"/>
<text class="badge2" x="${p.x+W/2-12}" y="${p.y-H/2+13.5}" text-anchor="middle" fill="#0d1117">${D.load[n.key]}</text>`;
g+=`</g>`;});
const svg=document.getElementById('fs');
svg.setAttribute('width',maxX-minX+80);svg.setAttribute('height',maxY-minY+90);
svg.innerHTML=g;
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));b.classList.add('on');
['orders','flow','skus'].forEach(v=>document.getElementById(v).style.display=v===b.dataset.v?'':'none');});
</script></body></html>"""


def main() -> None:
    html = HTML.replace("__DATA__", json.dumps(collect(), ensure_ascii=False))
    out = PROJECT_ROOT / "docs" / "data-viewer.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[export_data_html] 已生成 {out}")


if __name__ == "__main__":
    main()
