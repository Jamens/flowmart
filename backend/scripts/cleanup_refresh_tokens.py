"""清理 refresh_tokens 里的死记录（供 cron / 计划任务定期执行）。

为什么需要：轮转机制下，每次登录、每次刷新都会新增一行；只增不删会让表无限膨胀。

但**不能一刀切全删**——「已轮换（used_at 非空）但尚未过期」的行必须保留：
重放检测依赖它。攻击者重放旧令牌时，必须能查到这条记录才能判定为「重放」
并撤销整条 family；若提前删掉，重放只会落到「查无此记录」分支，
虽然同样是 401，却拿不到「撤销整条轮转链」这一更强的处置。

因此本脚本只删两类确定安全的行：
  1. **已过期**（expires_at < now）：JWT 的 exp 也已过，本来就不可能被接受；
  2. **已撤销且超过保留期**（默认 30 天）：整条 family 已死，留着仅供审计。

用法（建议每天跑一次）：
    python scripts/cleanup_refresh_tokens.py                      # 实际清理
    python scripts/cleanup_refresh_tokens.py --dry-run            # 只报告将删多少
    python scripts/cleanup_refresh_tokens.py --revoked-retention-days 7
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

# 让脚本可以直接运行：把 backend/ 加入模块搜索路径
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import and_, create_engine, delete, func, or_, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import utcnow_naive  # noqa: E402
from app.models.ecommerce import RefreshToken  # noqa: E402


def cleanup_refresh_tokens(
    session,
    now=None,
    revoked_retention_days: int = 30,
    dry_run: bool = False,
) -> dict:
    """删除过期行与超保留期的已撤销行，返回各类计数。

    now 可注入，便于测试用固定时间断言（默认取当前 UTC）。
    删除用「一条带 OR 条件的 DELETE」而非「先查出 id 列表再 id IN (...)」：
    后者在大表上会因绑定参数数量触到 SQLite/PG 上限而直接报错，
    还要把全部 id 拉进内存、并让一条巨型 DELETE 长时间占写锁。
    """
    if now is None:
        now = utcnow_naive()

    expired_cond = RefreshToken.expires_at < now
    cutoff = now - timedelta(days=revoked_retention_days)
    revoked_cond = and_(
        RefreshToken.revoked_at.is_not(None),
        RefreshToken.revoked_at < cutoff,
    )
    # 两类可能重叠（既过期又被撤销过），用 OR 一次命中并集，避免重复计算
    either = or_(expired_cond, revoked_cond)

    def _count(cond):
        return session.scalar(
            select(func.count()).select_from(RefreshToken).where(cond)
        ) or 0

    expired_count = _count(expired_cond)
    revoked_count = _count(revoked_cond)
    total = _count(either)

    if dry_run:
        return {
            "expired": expired_count,
            "revoked_stale": revoked_count,
            "deleted": 0,
            "would_delete": total,
        }

    session.execute(delete(RefreshToken).where(either))
    session.commit()
    return {
        "expired": expired_count,
        "revoked_stale": revoked_count,
        "deleted": total,
        "would_delete": total,
    }


def _build_engine():
    url = settings.database_url
    if settings.dialect == "sqlite":
        return create_engine(url, future=True)
    return create_engine(url, future=True, pool_pre_ping=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="清理过期的刷新令牌记录（refresh_tokens）")
    parser.add_argument("--dry-run", action="store_true", help="只报告将删除多少行，不实际删除")
    parser.add_argument(
        "--revoked-retention-days",
        type=int,
        default=30,
        help="已撤销记录的保留天数，超过才删除（默认 30；0 表示撤销后立即删除）",
    )
    args = parser.parse_args(argv)
    # 负数会让「刚撤销」的记录也被清掉，多半是手误，直接拒绝
    if args.revoked_retention_days < 0:
        parser.error("--revoked-retention-days 不能为负数")

    engine = _build_engine()
    Session = sessionmaker(bind=engine, future=True)
    try:
        with Session() as s:
            result = cleanup_refresh_tokens(
                s,
                revoked_retention_days=args.revoked_retention_days,
                dry_run=args.dry_run,
            )
    except Exception as exc:  # 让 cron 能从退出码看出失败
        print(f"[cleanup] 清理失败：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    if args.dry_run:
        print(
            f"[cleanup] [dry-run] 过期 {result['expired']} 条、"
            f"撤销超保留期 {result['revoked_stale']} 条，将删除 {result['would_delete']} 条"
        )
    else:
        print(
            f"[cleanup] 过期 {result['expired']} 条、"
            f"撤销超保留期 {result['revoked_stale']} 条，已删除 {result['deleted']} 条"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
