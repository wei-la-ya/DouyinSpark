"""会话列表拉取：imapi get_message_by_init（protobuf，Cookie 鉴权）。

忠实移植自 douyin-id-spark 的 conversation-api.js：
从主收件箱（好友私信）1v1 会话的参与者中取对方 sec_uid/uid。
协议为 2026-09 抓包校正（cmd=2043），纯 API，无浏览器。

与 JS 版差异：
- profileFetchLimit 不再读 Yunzai config，改为 list_conversations 的参数；
- uid / cursor 等 int64 字段为 Python int（JS 版为字符串，值等价）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar

import httpx

from .api import (
    DouyinApiError,
    build_cookie_header,
    fetch_user_profile,
    get_cookie_value,
    post_im_proto_raw,
)
from .im_proto import build_get_by_user_init_body, parse_get_by_user_init_response

GET_MESSAGE_BY_INIT_PATH = "/v1/message/get_message_by_init"
MAX_PAGES = 10
MAX_PROFILE_FETCH = 30
PROFILE_CONCURRENCY = 5
PAGE_INTERVAL = 0.5  # 秒（JS PAGE_INTERVAL_MS = 500）

__all__ = [
    "map_with_concurrency",
    "extract_people",
    "fetch_init_pages",
    "fetch_inbox_overview",
    "list_conversations",
]

_T = TypeVar("_T")
_R = TypeVar("_R")


async def map_with_concurrency(
    items: Sequence[_T],
    limit: int,
    fn: Callable[[_T, int], Awaitable[_R]],
) -> list[_R]:
    """并发池：最多 limit 并发执行 fn，保持输入顺序返回结果。"""
    semaphore = asyncio.Semaphore(max(1, min(limit, len(items)) or 1))

    async def run(item: _T, index: int) -> _R:
        async with semaphore:
            return await fn(item, index)

    return list(await asyncio.gather(*(run(item, i) for i, item in enumerate(items))))


def extract_people(pages: Sequence[dict[str, Any]], self_uid: int) -> list[dict[str, Any]]:
    """从多页会话中提取「会话里出现过的人」。

    规则：仅保留 1v1 单聊（conversation_type=1 且 participants_count=2）；
    对方 = 参与者中 uid 不等于自己的那个（参与者信息里直接带 sec_uid）。
    self_uid 为 0 时无法判定对方侧，返回空列表。
    """
    if not self_uid:
        return []
    people: dict[str, dict[str, Any]] = {}
    for page in pages:
        for conversation in page.get("conversations", []):
            if conversation.get("conversation_type") != 1:
                continue
            if conversation.get("participants_count", 0) != 2:
                continue
            peer = next(
                (
                    user
                    for user in conversation.get("participants", [])
                    if user.get("uid") and user["uid"] != self_uid
                ),
                None,
            )
            if not peer or not peer.get("sec_uid"):
                continue
            if peer["sec_uid"] in people:
                continue
            people[peer["sec_uid"]] = {
                "sec_uid": peer["sec_uid"],
                "uid": peer["uid"],
                "conversation_id": conversation["conversation_id"],
                "conversation_short_id": conversation["conversation_short_id"],
                "ticket": conversation["ticket"],
            }
    return list(people.values())


async def fetch_init_pages(
    cookie_header: str,
    *,
    on_progress: Optional[Callable[[str], Any]] = None,
    max_pages: int = MAX_PAGES,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """分页拉取 get_message_by_init，返回 {pages, self_uid}。"""
    progress = on_progress or (lambda _msg: None)
    pages: list[dict[str, Any]] = []
    cursor: Any = "0"
    self_uid = 0
    for index in range(max_pages):
        progress(f"正在拉取会话列表（第 {index + 1} 页）…")
        body = build_get_by_user_init_body(cursor=cursor, sequence_id=10001 + index)
        bytes_ = await post_im_proto_raw(
            GET_MESSAGE_BY_INIT_PATH, cookie_header, body, "拉取会话列表", client=client
        )
        try:
            parsed = parse_get_by_user_init_response(bytes_)
        except Exception as error:
            # 响应解析失败：多半是 Cookie 失效 / 风控命中导致返回了非 protobuf 内容
            # 把响应头和前几字节打印出来，用户能据此判断下一步动作
            preview_hex = bytes_[:64].hex() if bytes_ else ""
            preview_text = ""
            try:
                preview_text = bytes_[:200].decode("utf-8", errors="replace") if bytes_ else ""
            except Exception:
                pass
            from .api import _LAST_RESPONSE_CONTENT_TYPE  # type: ignore[attr-defined]
            ct = _LAST_RESPONSE_CONTENT_TYPE
            detail = (
                f"响应解析失败（{error}）；content-type={ct or '?'}；"
                f"前64字节hex={preview_hex}；前200字节utf8={preview_text!r}"
            )
            logger.warning(f"[DouyinSpark] {detail}")
            raise DouyinApiError(f"拉取会话列表失败：{detail}", kind="parse") from error
        if parsed["self_uid"]:
            self_uid = parsed["self_uid"]
        pages.append(parsed)
        if not parsed["has_more"] or not parsed["next_cursor"]:
            break
        cursor = parsed["next_cursor"]
        if index < max_pages - 1:
            await asyncio.sleep(PAGE_INTERVAL)
    return {"pages": pages, "self_uid": self_uid}


async def fetch_inbox_overview(
    cookie_header: str, *, client: Optional[httpx.AsyncClient] = None
) -> dict[str, Any]:
    """拉取收件箱总览（续火执行器用）：

    返回 {self_uid, by_sec_uid}，by_sec_uid 按对方 sec_uid 索引会话信息
    （含最近一条自发消息时间 last_self_message_ms，用于「今天已续过」过滤）。
    """
    result = await fetch_init_pages(cookie_header, client=client)
    pages, self_uid = result["pages"], result["self_uid"]
    by_sec_uid: dict[str, dict[str, Any]] = {}
    for page in pages:
        for conversation in page.get("conversations", []):
            if conversation.get("conversation_type") != 1:
                continue
            peer = next(
                (
                    user
                    for user in conversation.get("participants", [])
                    if user.get("uid") and user["uid"] != self_uid
                ),
                None,
            )
            if not peer or not peer.get("sec_uid"):
                continue
            last_self_message_ms = 0
            last_message_ms = 0
            for message in conversation.get("messages", []):
                ms = message.get("client_create_time") or 0
                if ms > last_message_ms:
                    last_message_ms = ms
                if message.get("sender") == self_uid and ms > last_self_message_ms:
                    last_self_message_ms = ms
            by_sec_uid[peer["sec_uid"]] = {
                "conversation_id": conversation["conversation_id"],
                "conversation_short_id": conversation["conversation_short_id"],
                "ticket": conversation["ticket"],
                "last_self_message_ms": last_self_message_ms,
                "last_message_ms": last_message_ms,
            }
    return {"self_uid": self_uid, "by_sec_uid": by_sec_uid}


async def list_conversations(
    cookies: Sequence[dict[str, Any]],
    *,
    on_progress: Optional[Callable[[str], Any]] = None,
    profile_fetch_limit: int = MAX_PROFILE_FETCH,
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict[str, Any]]:
    """拉取账号会话列表中出现过的用户（昵称通过 profile 接口逐个补全）。"""
    progress = on_progress or (lambda _msg: None)
    cookie_header = build_cookie_header(cookies)

    result = await fetch_init_pages(cookie_header, on_progress=on_progress, client=client)
    pages, self_uid = result["pages"], result["self_uid"]
    if len(pages) >= MAX_PAGES:
        progress(f"已达 {MAX_PAGES} 页上限，仅返回最近的部分会话")

    if not self_uid:
        raise DouyinApiError("未能从会话列表响应中识别当前账号 uid", kind="api")
    people = extract_people(pages, self_uid)
    if not people:
        raise DouyinApiError("没有解析到可选择的 1v1 会话（该账号可能没有私信记录）", kind="api")

    # 昵称补全：响应不含昵称，并发查 profile（默认 5 并发）；超上限的条目昵称留空（前端仅显示 ID）
    fetch_limit = profile_fetch_limit or MAX_PROFILE_FETCH
    progress(f"已发现 {len(people)} 个会话用户，正在获取昵称（上限 {fetch_limit} 人）…")
    webid = get_cookie_value(cookies, "s_v_web_id")
    uifid = get_cookie_value(cookies, "UIFID")
    fetch_targets = people[:fetch_limit]

    async def fill(person: dict[str, Any], _index: int) -> dict[str, Any]:
        try:
            profile = await fetch_user_profile(
                cookie_header, person["sec_uid"], webid=webid, uifid=uifid, client=client
            )
            person["nickname"] = profile["nickname"]
            if profile["uid"]:
                person["uid"] = profile["uid"]
            if profile["unique_id"]:
                person["unique_id"] = profile["unique_id"]
            if profile["avatar"]:
                person["avatar"] = profile["avatar"]
        except Exception:
            person["nickname"] = ""
        return person

    await map_with_concurrency(fetch_targets, PROFILE_CONCURRENCY, fill)
    return [{**person, "nickname": person.get("nickname") or ""} for person in people]
