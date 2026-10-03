import os
import tempfile

import pytest

# 必须在导入 app.config 之前指定测试库路径
_tmpdir = tempfile.mkdtemp(prefix="router-test-")
os.environ["ROUTER_DB_PATH"] = os.path.join(_tmpdir, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_conn, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_db():
    """每个用例之间清空规则与版本，修订号归零。"""
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM version_targets")
        conn.execute("DELETE FROM version_rules")
        conn.execute("DELETE FROM versions")
        conn.execute("DELETE FROM draft_targets")
        conn.execute("DELETE FROM draft_rules")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('versions','draft_rules')"
        )
        conn.execute("UPDATE draft_meta SET revision = 0 WHERE id = 1")
        conn.commit()
    yield
