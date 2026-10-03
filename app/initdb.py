"""独立初始化入口：python -m app.initdb

创建/升级 SQLite 库表并写入初始草稿修订号。幂等，可重复执行。
"""
from .db import init_db


def main() -> None:
    init_db()
    print("database initialized")


if __name__ == "__main__":
    main()
