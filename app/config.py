"""运行期配置，全部可通过环境变量覆盖。"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("ROUTER_DB_PATH", "/data/router.db")
    host: str = os.getenv("ROUTER_HOST", "0.0.0.0")
    port: int = int(os.getenv("ROUTER_PORT", "8080"))
    # 单次展开允许经过的最大别名跳数，兜底防御
    max_hops: int = int(os.getenv("ROUTER_MAX_HOPS", "100"))


settings = Settings()
