"""一言（本地离线语料）"""
import json
import random
from typing import TypedDict

import aiofiles

from .resource import YIYAN_PATH


class Yiyan(TypedDict):
    hitokoto: str
    from_: str


_cache: list[dict] | None = None


async def load_yiyans() -> list[dict]:
    """加载一言语料（hitokoto 格式数组），带进程内缓存"""
    global _cache
    if _cache is None:
        async with aiofiles.open(YIYAN_PATH, encoding="utf-8") as f:
            data = json.loads(await f.read())
        if not isinstance(data, list) or not data:
            raise ValueError("assets 一言语料为空")
        _cache = data
    return _cache


async def pick_yiyan() -> dict:
    """随机取一条一言"""
    return random.choice(await load_yiyans())
