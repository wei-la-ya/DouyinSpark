"""路径常量"""
import sys
from pathlib import Path

from gsuid_core.data_store import get_res_path

# 插件运行时根目录：data/DouyinSpark/
MAIN_PATH = get_res_path() / "DouyinSpark"
sys.path.append(str(MAIN_PATH))

CONFIG_PATH = MAIN_PATH / "config.json"
CACHE_PATH = MAIN_PATH / "cache"

CACHE_PATH.mkdir(parents=True, exist_ok=True)

# 插件自带静态资源（随代码分发，只读）
PLUGIN_RES = Path(__file__).parent
YIYAN_PATH = PLUGIN_RES / "yiyan.json"
