"""flowmart 运维脚本集合：init_db（建库建表/迁移）、seed（灌演示数据）。

作为 app 的子包，便于测试中以 `from app.scripts.seed import seed_users` 形式导入。
直接运行仍用 `python scripts/seed.py`，__init__ 不影响脚本执行。
"""
