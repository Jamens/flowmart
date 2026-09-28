"""接口契约快照：**防止后端改了接口形状、而前端完全不知情**。

为什么需要它（单测与端到端都补不上这个洞）：
- 单测断言的是「我知道该断言什么」，字段改名后我也会跟着改断言 → 照样绿；
- 端到端脚本同理，脚本是我写的，接口变了我会同步改脚本 → 照样绿；
- 但**前端不是我改的**：后端把 `order_id` 改成 `id`、给某个响应加了个必填字段，
  前端 `api.js` / 视图还在按旧形状取值，运行时才炸，而且往往炸在用户身上。

所以这里把**接口定义本身**当基准：任何 path / 参数 / 请求体 / 响应 schema 的
变化都必须显式确认，不能悄悄进 main。

刻意只快照「形状」而非整个 openapi.json：
summary / description 这类文案会随注释频繁变动，纳进来会让快照天天需要更新，
最后大家就养成 `UPDATE=1` 无脑重生成的习惯——那这个测试就死了。

更新快照（确认变更是有意的之后）：
    OPENAPI_UPDATE=1 python -m pytest backend/tests/test_openapi_snapshot.py -q
"""
import json
import os
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "openapi-snapshot.json"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _schema_of(content: dict) -> str | None:
    """取请求/响应的 schema 标识：$ref 优先，否则退回到 type。"""
    for media in (content or {}).values():
        schema = media.get("schema") or {}
        if schema.get("$ref"):
            return schema["$ref"]
        if schema.get("type"):
            return schema["type"]
    return None


def normalized(spec: dict) -> dict:
    """把 openapi 规约压成「形状」字典：只看路径、参数、请求体与响应的 schema。"""
    out = {}
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in (methods or {}).items():
            if method not in HTTP_METHODS:
                continue
            # 用 list 而非 tuple：JSON 往返会把 tuple 变成 list，
            # 两侧类型不一致会让「每次都判定为变更」，快照就失去意义了
            params = sorted(
                [p.get("name"), p.get("in"), bool(p.get("required", False))]
                for p in (op.get("parameters") or [])
            )
            request = _schema_of((op.get("requestBody") or {}).get("content"))
            responses = {
                str(code): _schema_of((r or {}).get("content"))
                for code, r in (op.get("responses") or {}).items()
            }
            out[f"{method.upper()} {path}"] = {
                "params": params,
                "request": request,
                "responses": dict(sorted(responses.items())),
            }
    return dict(sorted(out.items()))


def test_openapi_contract_matches_snapshot(raw_client):
    spec = raw_client.get("/openapi.json").json()
    actual = normalized(spec)

    if os.getenv("OPENAPI_UPDATE") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n[openapi] 已更新快照：{SNAPSHOT}")
        return

    if not SNAPSHOT.exists():
        raise AssertionError(
            f"快照不存在：{SNAPSHOT}。确认接口形状后运行 "
            "OPENAPI_UPDATE=1 python -m pytest backend/tests/test_openapi_snapshot.py -q"
        )

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(k for k in set(actual) & set(expected) if actual[k] != expected[k])

    assert not (added or removed or changed), (
        "接口契约发生变化，前端可能受影响。确认是有意变更后再更新快照：\n"
        f"  新增：{added}\n  移除：{removed}\n  变更：{changed}\n"
        "更新命令：OPENAPI_UPDATE=1 python -m pytest "
        "backend/tests/test_openapi_snapshot.py -q"
    )
