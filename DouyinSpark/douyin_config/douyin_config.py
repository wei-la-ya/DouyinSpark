"""DouyinSpark 配置实例"""
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import CONFIG_DEFAULT

CONFIG_PATH = get_res_path() / "DouyinSpark" / "config.json"

# 全局单例
dy_config = StringConfig("DouyinSpark", CONFIG_PATH, CONFIG_DEFAULT)
