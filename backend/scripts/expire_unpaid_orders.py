"""超时未支付订单自动取消并归还库存（供 cron / 计划任务定期执行）。

为什么需要：下单即预占库存（原子扣减），若用户一直不付款，库存就被这条订单永久锁死，
别人买不了。真实电商都有「支付时限」（常见 15/30 分钟）+ 超时自动取消回滚。

边界：工作流引擎本身**没有定时/延时事件能力**，本脚本就是那个「外部定时器」——
靠 cron 周期性轮询，把超过支付窗的待付款订单用既有的 cancel 副作用退回库存。

只处理 `pending_payment` 且 `created_at` 早于 `now - timeout` 的订单；cancel 副作用会
归还库存，未支付流水标记为 expired 以免对账时把死流水当待支付。已取消/已支付订单
自然不在筛选范围内，因此本脚本**天然幂等**：反复跑不会重复归还库存。

注意时间基准：用 `utcnow_naive()`（UTC）与 `created_at`（DB server_default=now()）比较，
与项目其余时间戳保持一致——前提是数据库服务器时钟为 UTC（生产 MySQL 通常如此）。

用法（建议每 1~5 分钟跑一次，远小于支付窗）：
    python scripts/expire_unpaid_orders.py
    python scripts/expire_unpaid_orders.py --dry-run
    python scripts/expire_unpaid_orders.py --timeout-minutes 15
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

# 让脚本可以直接运行：把 backend/ 加入模块搜索路径
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, select, update  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import utcnow_naive  # noqa: E402
from app.models.ecommerce import Order, Payment  # noqa: E402
from app.services.order_service import OrderService  # noqa: E402


def expire_unpaid_orders(
    session,
    now=None,
    timeout_minutes=None,
    dry_run: bool = False,
) -> dict:
    """取消超时未支付订单并归还库存，返回计数。

    now / timeout_minutes 可注入，便于测试用固定时间断言；timeout_minutes 默认取
    settings.ORDER_PAY_TIMEOUT_MINUTES。<=0 表示不启用，直接返回空结果。
    """
    if now is None:
        now = utcnow_naive()
    if timeout_minutes is None:
        timeout_minutes = settings.ORDER_PAY_TIMEOUT_MINUTES
    if timeout_minutes <= 0:
        return {"expired": 0, "would_expire": 0, "disabled": True}

    cutoff = now - timedelta(minutes=timeout_minutes)
    orders = (
        session.execute(
            select(Order).where(
                Order.current_node_key == "pending_payment",
                Order.created_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    count = len(orders)

    if dry_run:
        return {"expired": 0, "would_expire": count, "disabled": False}

    svc = OrderService(session)
    for order in orders:
        # 先把这条订单上还没支付的流水标记为过期，避免对账把死流水当成待支付。
        session.execute(
            update(Payment)
            .where(Payment.order_id == order.id, Payment.status == "pending")
            .values(status="expired")
        )
        # cancel 副作用会原子归还库存，并把订单推进到 closed。
        svc.trigger(
            order.id, "cancel", operator="system:timeout", comment="超时未支付自动取消"
        )
    return {"expired": count, "would_expire": count, "disabled": False}


def _build_engine():
    url = settings.database_url
    if settings.dialect == "sqlite":
        return create_engine(url, future=True)
    return create_engine(url, future=True, pool_pre_ping=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="超时未支付订单自动取消并归还库存")
    parser.add_argument("--dry-run", action="store_true", help="只报告将取消多少单，不实际改动")
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=None,
        help=(
            f"支付窗时长（分钟），默认取 ORDER_PAY_TIMEOUT_MINUTES"
            f"（当前 {settings.ORDER_PAY_TIMEOUT_MINUTES}）；<=0 表示不启用"
        ),
    )
    args = parser.parse_args(argv)

    if (args.timeout_minutes or settings.ORDER_PAY_TIMEOUT_MINUTES) <= 0:
        print("[expire] 超时未启用（timeout<=0），跳过")
        return 0

    engine = _build_engine()
    Session = sessionmaker(bind=engine, future=True)
    try:
        with Session() as s:
            result = expire_unpaid_orders(
                s,
                timeout_minutes=args.timeout_minutes,
                dry_run=args.dry_run,
            )
    except Exception as exc:  # 让 cron 能从退出码看出失败
        print(f"[expire] 执行失败：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    if args.dry_run:
        print(f"[expire] [dry-run] 将取消 {result['would_expire']} 笔超时未支付订单")
    else:
        print(f"[expire] 已取消 {result['expired']} 笔超时未支付订单并归还库存")
    return 0


if __name__ == "__main__":
    sys.exit(main())
