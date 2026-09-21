"""非管理员客户端（buyer_client）覆盖验证。

conftest 的默认 current_user 是管理员，旧测试因此都跑在管理员身份下；
新增 buyer_client 让买家侧行为也能在「依赖注入覆盖」路径下被测试，
与 test_rbac 里 raw_client 的真实鉴权路径互补，闭合 reviewer 提到的买家路径覆盖缺口。
"""
from app.core.security import hash_password
from app.models.ecommerce import User


def test_buyer_cannot_list_users(buyer_client):
    """普通买家访问用户列表（管理员专属）必须 403。"""
    assert buyer_client.get("/api/v1/users").status_code == 403


def test_buyer_cannot_read_or_edit_other_user(buyer_client, db):
    """买家改/读他人资料必须 404（不泄露目标是否存在），与 raw_client 路径互补验证。"""
    other = User(
        username="victim", nickname="受害者", phone="13800000007",
        password_hash=hash_password("123456"), is_admin=False,
    )
    db.add(other)
    db.commit()
    db.refresh(other)

    assert buyer_client.get(f"/api/v1/users/{other.id}").status_code == 404
    assert (
        buyer_client.patch(f"/api/v1/users/{other.id}", json={"nickname": "x"}).status_code
        == 404
    )
