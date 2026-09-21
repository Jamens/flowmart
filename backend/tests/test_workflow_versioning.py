"""工作流版本管理 API 测试。

覆盖：
1. 派生新版本（克隆图，version = max+1）
2. 版本历史接口
3. 发布时保证全局唯一 published（降级同 code 其它 published 版本）
4. 回滚 = 重新发布旧版本
5. 列表 ?code= 过滤
6. 在途实例按 definition_id 钉死，发布新版本不影响其继续流转
"""
WF = "/api/v1/workflows/definitions"


def test_fork_creates_new_version_and_clones_graph(client, order_flow):
    """从已发布版本派生 v2 草稿，节点与流转边原样克隆。"""
    r = client.post(f"{WF}/{order_flow.id}/versions")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["status"] == "draft"
    assert body["source_id"] == order_flow.id
    assert body["cloned_nodes"] == 6  # order_flow fixture 共 6 个节点

    # 新版本可独立读取，且图结构与源一致
    new = client.get(f"{WF}/{body['id']}").json()
    assert len(new["nodes"]) == 6
    assert len(new["transitions"]) == 6
    assert new["code"] == order_flow.code


def test_version_history_endpoint(client, order_flow):
    """版本历史按 version 倒序列出全部版本（含 draft/published/archived）。"""
    fork = client.post(f"{WF}/{order_flow.id}/versions").json()
    client.post(f"{WF}/{fork['id']}/publish")  # v2 published，v1 archived

    hist = client.get(f"{WF}/code/{order_flow.code}/versions").json()
    assert [h["version"] for h in hist] == [2, 1]
    by_v = {h["version"]: h for h in hist}
    assert by_v[1]["status"] == "archived"
    assert by_v[2]["status"] == "published"


def test_version_history_404_for_unknown_code(client):
    r = client.get(f"{WF}/code/not_exist/versions")
    assert r.status_code == 404


def test_fork_nonexistent_definition_404(client):
    r = client.post(f"{WF}/999999/versions")
    assert r.status_code == 404


def test_republish_already_published_is_noop(client, order_flow):
    """重新发布已处于 published 的版本：没有同 code 的其它 published 版本，demoted 应为空。"""
    r = client.post(f"{WF}/{order_flow.id}/publish")
    assert r.status_code == 200
    assert r.json()["demoted"] == []


def test_fork_from_draft_source(client, db):
    """派生不只限已发布版本：从 draft 源也能克隆出 v2。"""
    created = client.post(
        f"{WF}",
        json={
            "code": "wf_draft",
            "name": "草案流程",
            "nodes": [
                {"key": "start", "name": "开始", "node_type": "start"},
                {"key": "end", "name": "结束", "node_type": "end"},
            ],
            "transitions": [{"from_node_key": "start", "to_node_key": "end", "event": "go"}],
        },
    ).json()
    r = client.post(f"{WF}/{created['id']}/versions")
    assert r.status_code == 201
    assert r.json()["version"] == 2
    assert r.json()["cloned_nodes"] == 2


def test_publish_demotes_sibling_published(client, order_flow):
    """发布 v2 时，同 code 已发布的 v1 必须降级为 archived（全局唯一 published）。"""
    fork = client.post(f"{WF}/{order_flow.id}/versions").json()
    r = client.post(f"{WF}/{fork['id']}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["demoted"] == [order_flow.id]

    # published 端点应返回最新版本（v2）
    pub = client.get(f"{WF}/code/{order_flow.code}").json()
    assert pub["version"] == 2


def test_rollback_by_publishing_old_version(client, order_flow):
    """回滚 = 把旧版本重新 publish：v2 被降级，v1 变回 published。"""
    fork = client.post(f"{WF}/{order_flow.id}/versions").json()
    client.post(f"{WF}/{fork['id']}/publish")  # v2 published, v1 archived

    # 重新发布 v1（旧版本）
    r = client.post(f"{WF}/{order_flow.id}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["demoted"] == [fork["id"]]

    hist = client.get(f"{WF}/code/{order_flow.code}/versions").json()
    by_v = {h["version"]: h for h in hist}
    assert by_v[1]["status"] == "published"
    assert by_v[2]["status"] == "archived"


def test_list_definitions_code_filter(client, order_flow):
    """?code= 只返回该业务 code 的版本行。"""
    client.post(f"{WF}/{order_flow.id}/versions")  # 制造一条 v2

    all_rows = client.get(f"{WF}").json()
    filtered = client.get(f"{WF}?code={order_flow.code}").json()
    assert len(filtered) == 2
    assert all(d["code"] == order_flow.code for d in filtered)
    assert len(all_rows) >= 2


def test_in_flight_instance_unaffected_by_publish(client, engine, order_flow):
    """在途实例钉死在 definition_id，发布新版本降级旧版本后仍能继续流转。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="v1", context={"amount": 500})
    assert inst.definition_id == order_flow.id

    # 派生并发布 v2（v1 降级为 archived）
    fork = client.post(f"{WF}/{order_flow.id}/versions").json()
    client.post(f"{WF}/{fork['id']}/publish")

    # 实例的 definition_id 必须保持不变
    inst2 = engine.db.get(type(inst), inst.id)
    assert inst2.definition_id == order_flow.id

    # 原实例仍可推进：走 submit → pending_payment（始终用 v1 的图）
    engine.fire(inst.id, "submit")
    assert inst.current_node_key == "pending_payment"
