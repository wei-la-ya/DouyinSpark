"""抖音 Web API 核心接口封装（异步 httpx 版）。

所有接口均为逆向所得，抖音风控升级后可能需要更新签名文件或请求模板。

返回 dict 的键统一为 snake_case；int64 字段为 Python int。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional, Sequence, Union
from urllib.parse import quote, urljoin

import httpx

from .abogus import gen_ms_token, gen_verify_fp, sign_abogus
from .im_proto import (
    build_create_conversation_body,
    build_text_message_body,
    parse_im_response,
)

# 用于非 imapi 的 web REST 端点（如 user_info 探测）的 UA
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

IMAPI_BASE = "https://imapi.douyin.com"
# 最近一次 IM 响应的 content-type（conversations 解析失败时用于诊断 cookie/风控）
_LAST_RESPONSE_CONTENT_TYPE: Optional[str] = None
PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
REQUEST_TIMEOUT = 20.0  # 秒（JS REQUEST_TIMEOUT_MS = 20000）

__all__ = [
    "USER_AGENT",
    "DouyinApiError",
    "build_cookie_header",
    "get_cookie_value",
    "post_im_proto",
    "post_im_proto_raw",
    "resolve_share_link",
    "fetch_user_profile",
    "create_conversation",
    "send_text_message",
    "random_delay",
]


class DouyinApiError(Exception):
    """kind: 'auth' | 'risk' | 'network' | 'api' | 'parse' | 'unknown'"""

    def __init__(
        self,
        message: str,
        kind: str = "unknown",
        status_code: Optional[int] = None,
        status_msg: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.status_msg = status_msg


def build_cookie_header(cookies: Sequence[dict[str, Any]]) -> str:
    """Cookie-Editor JSON 数组 -> douyin.com 域名的 cookie 字符串。"""
    if not cookies:
        raise DouyinApiError("Cookie 数据为空", kind="auth")
    pairs = []
    for cookie in cookies:
        if not cookie or not cookie.get("name") or cookie.get("value") is None:
            continue
        domain = str(cookie.get("domain") or "")
        if "douyin.com" not in domain:
            continue
        pairs.append(f"{cookie['name']}={cookie['value']}")
    if not pairs:
        raise DouyinApiError("Cookie 中没有 douyin.com 域名的条目", kind="auth")
    return "; ".join(pairs)


def get_cookie_value(cookies: Sequence[dict[str, Any]], name: str) -> str:
    """从 Cookie 中取指定 name 的值。"""
    for cookie in cookies:
        if cookie and cookie.get("name") == name:
            return str(cookie.get("value", ""))
    return ""


def _im_headers(cookie_header: str) -> dict[str, str]:
    """imapi 发送用 4 个 header（最小集）。

    加 Origin / sec-fetch-* / accept-language 等会触发服务端风控返回 StatusCode_130。
    """
    from .im_proto import WEB_PC_UA, WEB_REFERER
    return {
        "cookie": cookie_header,
        "content-type": "application/x-protobuf",
        "user-agent": WEB_PC_UA,
        "referer": WEB_REFERER,
    }


def _assert_http_ok(response: httpx.Response, action: str) -> None:
    if response.status_code in (401, 403):
        raise DouyinApiError(
            f"{action}：认证失败（HTTP {response.status_code}），Cookie 可能已失效",
            kind="auth",
            status_code=response.status_code,
        )
    if response.status_code == 429:
        raise DouyinApiError(
            f"{action}：请求频率超限（HTTP 429），请稍后重试", kind="risk", status_code=429
        )
    if response.status_code >= 400:
        raise DouyinApiError(
            f"{action}：HTTP 请求失败，状态码 {response.status_code}",
            kind="network",
            status_code=response.status_code,
        )


@asynccontextmanager
async def _client_ctx(client: Optional[httpx.AsyncClient]) -> AsyncIterator[httpx.AsyncClient]:
    """允许调用方注入共享 client；缺省时按调用创建（对齐 JS 无状态 fetch）。"""
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT), follow_redirects=False
        ) as owned:
            yield owned


async def post_im_proto(
    path: str,
    cookie_header: str,
    body: bytes,
    action: str,
    *,
    signed: bool = True,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    bytes_ = await post_im_proto_raw(path, cookie_header, body, action, signed=signed, client=client)
    try:
        parsed = parse_im_response(bytes_)
    except Exception as error:
        raise DouyinApiError(f"{action}：响应解析失败（{error}）", kind="parse") from error
    # 抖音 IM send/create 响应：f1=status_code，0=OK，100=业务错误（带 status_message）
    # 注意：错误响应里 f1=100、f4=status_message=StatusCode_130，不能只看 status_message=="OK"
    if parsed.get("status_code", 0) != 0 or parsed.get("status_message") != "OK":
        detail = parsed.get("status_message") or "未知错误"
        err_code = parsed.get("error_code") or 0
        extra = parsed.get("extra_info") or {}
        extra_code = extra.get("status_code") if isinstance(extra, dict) else None
        # 异步风控详情（有时 message_data.message_info.extra_info 内嵌 status_code=7905）
        if isinstance(extra_code, int) and extra_code >= 1000:
            # 异步风控拒绝 - 用 extra_code 报错
            extra_tip = ""
            if isinstance(extra, dict):
                msg = extra.get("status_msg") or {}
                content = msg.get("msg_content") or {}
                extra_tip = content.get("tips") or ""
            detail = extra_tip or detail
        tolerated = extra_code in (8101, 7174) or err_code in (8101, 7174)
        if not tolerated:
            kind = "auth" if re.search(r"登录|登录态|session", detail, re.I) else "api"
            code = extra_code if extra_code is not None else err_code
            raise DouyinApiError(
                f"{action}：{detail}" + (f"（状态码 {code}）" if code else ""),
                kind=kind,
                status_code=code,
                status_msg=detail,
            )
    return parsed


async def post_im_proto_raw(
    path: str,
    cookie_header: str,
    body: bytes,
    action: str,
    *,
    signed: bool = False,
    client: Optional[httpx.AsyncClient] = None,
) -> bytes:
    """发送 imapi protobuf 请求并返回原始字节（调用方按各自接口结构解析）。"""
    global _LAST_RESPONSE_CONTENT_TYPE
    url = f"{IMAPI_BASE}{path}"
    if signed:
        ms_token = gen_ms_token()
        fp = gen_verify_fp()
        query = f"msToken={quote(ms_token, safe='')}&verifyFp={quote(fp, safe='')}&fp={quote(fp, safe='')}"
        url += f"?{query}&a_bogus={quote(sign_abogus(query, USER_AGENT), safe='')}"
    async with _client_ctx(client) as http:
        try:
            response = await http.post(url, headers=_im_headers(cookie_header), content=body)
        except httpx.HTTPError as error:
            raise DouyinApiError(f"{action}：网络请求异常（{error}）", kind="network") from error
    _LAST_RESPONSE_CONTENT_TYPE = response.headers.get("content-type")
    _assert_http_ok(response, action)
    return response.content


async def resolve_share_link(
    text: str, *, client: Optional[httpx.AsyncClient] = None
) -> str:
    """从分享文本/链接中解析用户 sec_uid。

    支持：分享口令中的 v.douyin.com 短链、douyin.com/user/MS4w... 直链、
    iesdouyin.com/share/user 链接。返回 sec_uid。
    """
    input_text = str(text or "").strip()
    # 直接是完整链接的情况
    direct = re.search(
        r"(?:www\.douyin\.com|www\.iesdouyin\.com)/(?:share/)?user/(MS4w[\w-]+)", input_text
    ) or re.search(r"[?&]sec_uid=(MS4w[\w-]+)", input_text)
    if direct:
        return direct.group(1)

    short_match = re.search(r"https?://v\.douyin\.com/[\w-]+/?", input_text, re.I)
    if not short_match:
        raise DouyinApiError(
            "未识别到有效的抖音链接，请发送包含 v.douyin.com 短链的分享口令或用户主页链接",
            kind="parse",
        )

    # 手动跟随重定向链，逐跳检查 sec_uid
    url = short_match.group(0)
    async with _client_ctx(client) as http:
        for _hop in range(5):
            try:
                response = await http.get(
                    url, headers={"user-agent": USER_AGENT}, follow_redirects=False
                )
            except httpx.HTTPError as error:
                raise DouyinApiError(
                    f"解析分享链接失败：网络异常（{error}）", kind="network"
                ) from error
            location = response.headers.get("location")
            if not location:
                break
            next_url = location if location.startswith("http") else urljoin(url, location)
            found = re.search(r"/user/(MS4w[\w-]+)", next_url) or re.search(
                r"[?&]sec_uid=(MS4w[\w-]+)", next_url
            )
            if found:
                return found.group(1)
            url = next_url
    raise DouyinApiError(
        "未能从分享链接解析出用户 ID（sec_uid），该链接可能不是用户主页分享", kind="parse"
    )


async def fetch_user_profile(
    cookie_header: str,
    sec_uid: str,
    *,
    webid: str = "",
    uifid: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """通过 sec_uid 获取用户信息（uid / 昵称）。

    返回 {uid, sec_uid, nickname, unique_id, avatar}（uid 为 int）。
    """
    if not sec_uid or len(sec_uid) < 10 or len(sec_uid) > 100:
        raise DouyinApiError("sec_uid 格式无效", kind="parse")
    ms_token = gen_ms_token()
    fp = gen_verify_fp()
    # 参数顺序固定，签名对顺序敏感（a_bogus 计算的是这个字符串本身，顺序一致即可）
    pairs = [
        ("device_platform", "webapp"), ("aid", "6383"), ("channel", "channel_pc_web"),
        ("publish_video_strategy_type", "2"), ("source", "channel_pc_web"),
        ("sec_user_id", sec_uid), ("personal_center_strategy", "1"),
        ("profile_other_record_enable", "1"),
        ("land_to", "1"), ("update_version_code", "170400"), ("pc_client_type", "1"),
        ("pc_libra_divert", "Mac"), ("support_h265", "1"), ("support_dash", "1"),
        ("cpu_core_num", "8"), ("version_code", "170400"), ("version_name", "17.4.0"),
        ("cookie_enabled", "true"), ("screen_width", "3440"), ("screen_height", "1440"),
        ("browser_language", "zh-CN"), ("browser_platform", "MacIntel"),
        ("browser_name", "Chrome"),
        ("browser_version", "139.0.0.0"), ("browser_online", "true"),
        ("engine_name", "Blink"),
        ("engine_version", "139.0.0.0"), ("os_name", "Mac OS"), ("os_version", "10.15.7"),
        ("device_memory", "8"), ("platform", "PC"), ("downlink", "10"),
        ("effective_type", "4g"), ("round_trip_time", "50"),
    ]
    if webid:
        pairs.append(("webid", webid))
    if uifid:
        pairs.append(("uifid", uifid))
    pairs += [("verifyFp", fp), ("fp", fp), ("msToken", ms_token)]
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in pairs)
    url = f"{PROFILE_API}?{query}&a_bogus={quote(sign_abogus(query, USER_AGENT), safe='')}"

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cookie": cookie_header,
        "referer": f"https://www.douyin.com/user/{sec_uid}?from_tab_name=main",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": USER_AGENT,
    }
    async with _client_ctx(client) as http:
        try:
            response = await http.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise DouyinApiError(f"获取用户信息：网络请求异常（{error}）", kind="network") from error
    _assert_http_ok(response, "获取用户信息")
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise DouyinApiError(
            "获取用户信息：响应不是有效 JSON，可能触发风控验证", kind="risk"
        ) from error
    if data.get("status_code") != 0:
        msg = data.get("status_msg") or f"状态码 {data.get('status_code')}"
        kind = "auth" if re.search(r"登录|登录态", msg) else "api"
        raise DouyinApiError(
            f"获取用户信息失败：{msg}", kind=kind, status_code=data.get("status_code"), status_msg=msg
        )
    user = data.get("user")
    if not user or not user.get("uid"):
        raise DouyinApiError("获取用户信息失败：响应中没有用户数据", kind="api")
    avatar_larger = (user.get("avatar_larger") or {}).get("url_list") or []
    avatar_thumb = (user.get("avatar_thumb") or {}).get("url_list") or []
    return {
        "uid": int(user["uid"]),
        "sec_uid": sec_uid,
        "nickname": str(user.get("nickname") or ""),
        # 抖音号（用户可见的短 ID，如 douyin123）；未设置时回落 short_id
        "unique_id": str(user.get("unique_id") or user.get("short_id") or ""),
        "avatar": str((avatar_larger or avatar_thumb or [""])[0] or ""),
    }


async def create_conversation(
    cookie_header: str,
    *,
    receiver_uid: Union[str, int],
    sender_uid: Union[str, int, None] = None,
    template_b64: Optional[str] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    device_id: str = "0",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """创建/获取与对方的私信会话。返回 {conversation_id, conversation_short_id, self_uid}。"""
    body = build_create_conversation_body(
        receiver_uid=receiver_uid, sender_uid=sender_uid, template_b64=template_b64,
        device_id=device_id, self_uid=str(sender_uid or ""),
    )
    # 创建会话接口不签 a_bogus（与 send 不同）
    parsed = await post_im_proto(
        "/v2/conversation/create", cookie_header, body, "创建会话", signed=False, client=client
    )
    if not parsed["conversation_id"]:
        raise DouyinApiError("创建会话失败：响应中没有会话 ID", kind="api")
    return {
        "conversation_id": parsed["conversation_id"],
        "conversation_short_id": parsed["conversation_short_id"],
        "self_uid": parsed["self_uid"],
    }


async def send_text_message(
    cookie_header: str,
    *,
    conversation_id: str,
    conversation_short_id: Union[str, int],
    text: str,
    template_b64: Optional[str] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    device_id: str = "0",
    self_uid: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """发送文本私信。返回 {request_id}。"""
    client_message_id = str(uuid.uuid4())
    body = build_text_message_body(
        conversation_id=conversation_id,
        conversation_short_id=conversation_short_id,
        text=text,
        client_message_id=client_message_id,
        template_b64=template_b64,
        device_id=device_id,
        self_uid=self_uid,
    )
    parsed = await post_im_proto(
        "/v1/message/send", cookie_header, body, "发送私信", client=client
    )
    return {"request_id": parsed["request_id"]}


async def random_delay(min_sec: float, max_sec: float) -> None:
    """账号间/目标间随机延时（秒）。"""
    await asyncio.sleep(min_sec + random.random() * max(0.0, max_sec - min_sec))
