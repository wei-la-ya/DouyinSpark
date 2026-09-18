"""init"""
from gsuid_core.sv import Plugins

# Plugins 是单例：定义整个插件的前缀与权限
Plugins(
    name="DouyinSpark",
    force_prefix=["dy", "抖音"],
    allow_empty_prefix=False,
    alias=["douyin_spark", "dyspark"],
)

# 显式导入：注册 SV 命令处理器与 FastAPI 配置页路由
from . import douyin_api, douyin_spark  # noqa: E402, F401


# 框架的 SQLModel.metadata.create_all 只导入 CORE / AI 列出的模块；
# 插件表不在列，必须自己确保建表（参考 ai_core.state_store._ensure_table）。
# 否则首次安装后立刻 dy添加账号 会触发 no such table: dyaccount。
from sqlmodel import SQLModel  # noqa: E402
from gsuid_core.server import on_core_start_before  # noqa: E402
from gsuid_core.utils.database.base_models import engine  # noqa: E402

from .utils.database.models import DyAccount, DyTarget, DyUserPref  # noqa: E402


@on_core_start_before(priority=-50)
async def _ensure_douyin_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(
            SQLModel.metadata.create_all,
            tables=[DyAccount.__table__, DyTarget.__table__, DyUserPref.__table__],
            checkfirst=True,
        )
