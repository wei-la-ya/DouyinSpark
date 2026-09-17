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
