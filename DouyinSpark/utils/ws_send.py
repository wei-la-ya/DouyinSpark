"""抖音 IM WS 通道发送（安卓 frontier）。

为什么不用 HTTP imapi：HTTP imapi 通道要求服务端已注册过该 device_id（access_key）
的设备指纹，否则返回 StatusCode_130。Cookie 从浏览器导出时缺这套指纹。
WS 通道（安卓 frontier）每次连接时即时注册设备，是最可靠的发送路径。

帧结构（仿 jumpbyte-bot internal/engine/wssend.go）：
  外层(frontier)  f1 seq f2 ts f3 5 f4 1 f5 KV{cmd100..} f6/f7 "pb" f8=inner
  内层(cmd100)    f1 100 f2 seq f3 sdkver f5 1 f6 0 f7 build f8=msgWrapper
                   f9 uid f10 channel f11 android f12 device_type f13 os_ver f14 ver_code
                   f15 KV{app_name..} f18 0 f21 biz f22 access
  msgWrapper      f8 → length-delim 100 → field100{
                   f1 conv_id f2 conv_type f3 short f4 content_json
                   f5 ext_kv{...} f6 msg_type f8 cmid f12 ext12_kv{...}
                   }
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import random
import time
import uuid
from typing import Any, Optional

import websockets


AK_FPID = "9"
AK_APPKEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
AK_SALT = "f8a69f1719916z"
WS_BASE = "wss://frontier-aweme-lf-ipainner.amemv.com/ws/v2"

AWEME_AID = "1128"
AWEME_VERSION_CODE = "280400"
AWEME_VERSION_NAME = "28.4.0"
AWEME_UPDATE_VERSION_CODE = "28409900"
AWEME_CHANNEL = "douyinweb1_64"
AWEME_DEVICE_TYPE = "24031PN0DC"
AWEME_DEVICE_BRAND = "XIAOMI"
AWEME_OS_VERSION = "14"
AWEME_OS_API = "34"
AWEME_APP_NAME = "aweme"
AWEME_PACKAGE = "com.ss.android.ugc.aweme"
AWEME_UA = "okhttp/3.12.1 com.ss.android.ugc.aweme/280400"

WS_SEND_BIZ = "douyin"
WS_SEND_ACCESS = "douyin_main"
WS_SDK_VERSION = "5.0.3.0-rc.11-SNAPSHOT"
WS_BUILD_NUMBER = "5030"

WS_TEXT_CONTENT_KV: dict[str, Any] = {
    "type": 0,
    "instruction_type": 0,
    "item_type_local": -1,
    "createdAt": 0,
    "is_card": False,
    "msgHint": "",
}


# ===== wire helpers =====

def _write_varint_value(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _tag(field_num: int, wire_type: int) -> bytes:
    return _write_varint_value((field_num << 3) | wire_type)


def _field_varint(field_num: int, value: int) -> bytes:
    return _tag(field_num, 0) + _write_varint_value(value)


def _field_bytes(field_num: int, data: bytes) -> bytes:
    return _tag(field_num, 2) + _write_varint_value(len(data)) + data


def _field_str(field_num: int, s: str) -> bytes:
    return _field_bytes(field_num, s.encode("utf-8"))


def _kv_pair(field_num: int, key: str, value: str) -> bytes:
    inner = _field_str(1, key) + _field_str(2, value)
    return _field_bytes(field_num, inner)


def build_android_send_ext(ms: int) -> list[tuple[str, str]]:
    return [
        ("s:ticket_mode", "0"),
        ("im_client_send_msg_time", str(ms - 500)),
        ("a:plv", "1"),
        ("a:access", WS_SEND_ACCESS),
        ("s:biz_aid", AWEME_AID),
        ("chat_scene", "normal"),
        ("a:msg_scene", "1"),
        ("im_sdk_client_send_msg_time", str(ms - 375)),
        ("a:relation_type", "0:0"),
        ("a:smp_token_fetch", "11"),
        ("a:ntp_ready", "2"),
        ("s:sync_2_newdx", "1"),
        ("old_client_message_id", str(ms)),
        ("s:mode", "0"),
        ("a:enter_method", "click_message"),
        ("a:biz", WS_SEND_BIZ),
        ("s:is_stranger", "false"),
        ("source_aid", AWEME_AID),
        ("s:saas_sdk", "false"),
        ("a:sync2dx", "1"),
        ("s:refer", "3"),
    ]


ANDROID_SEND_EXT12 = [
    ("s:reverse_creator_im_ex", "0"),
    ("a:from_role_ids", ""),
    ("s:im_creator_chat_opt_exp", "0"),
    ("s:send_ignore_ticket", "true"),
    ("s:im_chat_priv_opt_exp", "1"),
    ("a:to_role_ids", "[]"),
]


def build_field100(
    *,
    conv_id: str,
    conv_type: int,
    short_id: int,
    text: str,
    msg_type: int,
    cmid: str,
) -> bytes:
    content_obj = dict(WS_TEXT_CONTENT_KV)
    content_obj["text"] = text
    content_obj["aweType"] = 700
    content_json = _json.dumps(content_obj, separators=(",", ":"), ensure_ascii=False)

    out = bytearray()
    out += _field_str(1, conv_id)
    out += _field_varint(2, conv_type)
    out += _field_varint(3, short_id)
    out += _field_str(4, content_json)

    ms = int(time.time() * 1000)
    for k, v in build_android_send_ext(ms):
        out += _kv_pair(5, k, v)

    out += _field_varint(6, msg_type)
    out += _field_str(8, cmid)

    for k, v in ANDROID_SEND_EXT12:
        out += _kv_pair(12, k, v)

    return bytes(out)


def build_msg_wrapper(field100: bytes) -> bytes:
    return _field_bytes(8, _field_bytes(100, field100))


def build_android_cmd100_inner(msg_wrapper: bytes, seq_id: int, device_id: str) -> bytes:
    out = bytearray()
    out += _field_varint(1, 100)
    out += _field_varint(2, seq_id)
    out += _field_str(3, WS_SDK_VERSION)
    out += _field_varint(5, 1)
    out += _field_varint(6, 0)
    out += _field_str(7, WS_BUILD_NUMBER)
    out += _field_bytes(8, msg_wrapper)
    out += _field_str(9, device_id)
    out += _field_str(10, AWEME_CHANNEL)
    out += _field_str(11, "android")
    out += _field_str(12, AWEME_DEVICE_TYPE)
    out += _field_str(13, AWEME_OS_VERSION)
    out += _field_str(14, AWEME_VERSION_CODE)
    kv = [
        ("app_name", AWEME_APP_NAME),
        ("iid", device_id),
        ("version_code", AWEME_VERSION_CODE),
        ("net_mcc_mnc", "46000"),
        ("aid", AWEME_AID),
        ("flow-tag", "new"),
        ("user-agent", AWEME_UA),
    ]
    for k, v in kv:
        out += _kv_pair(15, k, v)
    out += _field_varint(18, 0)
    out += _field_str(21, WS_SEND_BIZ)
    out += _field_str(22, WS_SEND_ACCESS)
    return bytes(out)


def wrap_outer_frame(inner: bytes, seq_id: int, ms: int) -> bytes:
    out = bytearray()
    out += _field_varint(1, seq_id)
    out += _field_varint(2, ms)
    out += _field_varint(3, 5)
    out += _field_varint(4, 1)
    seq_str = str(seq_id)
    for k, v in [
        ("msg_type", "cmd100"),
        ("seq_id", seq_str),
        ("cmd", "100"),
        ("is-retry", "0"),
        ("flow-tag", "new"),
    ]:
        out += _kv_pair(5, k, v)
    out += _field_str(6, "pb")
    out += _field_str(7, "pb")
    out += _field_bytes(8, inner)
    return bytes(out)


def build_ws_send_frame(
    *,
    conv_id: str,
    conv_type: int,
    short_id: int,
    text: str,
    device_id: str,
    msg_type: int = 7,
) -> tuple[bytes, str]:
    seq = random.randint(10000, 92220)
    cmid = str(uuid.uuid4())
    ms = int(time.time() * 1000)
    field100 = build_field100(
        conv_id=conv_id, conv_type=conv_type, short_id=short_id,
        text=text, msg_type=msg_type, cmid=cmid,
    )
    msg_wrapper = build_msg_wrapper(field100)
    inner = build_android_cmd100_inner(msg_wrapper, seq, device_id)
    frame = wrap_outer_frame(inner, seq, ms)
    return frame, cmid


# ===== response decoding =====

def decode_top(data: bytes) -> list[tuple[int, int, Any]]:
    """宽松 protobuf 解码：length-delim 字段先尝试当嵌套 message，否则当 string/bytes。"""
    out: list[tuple[int, int, Any]] = []
    pos = 0
    n = len(data)
    while pos < n:
        tag = 0
        shift = 0
        while pos < n:
            b = data[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = tag >> 3
        wire = tag & 7
        if field_num == 0:
            break
        if wire == 0:
            value = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                value |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            out.append((field_num, wire, value))
        elif wire == 2:
            length = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if length < 0 or pos + length > n:
                break
            chunk = data[pos:pos + length]
            pos += length
            # 尝试递归解嵌套 message
            nested: Any = None
            if length > 0:
                try:
                    parsed = decode_top(chunk)
                    if parsed:
                        nested = ("msg", parsed)
                except Exception:
                    nested = None
            if nested is not None:
                out.append((field_num, wire, nested))
            else:
                # 当 string 存
                try:
                    out.append((field_num, wire, chunk.decode("utf-8")))
                except UnicodeDecodeError:
                    out.append((field_num, wire, chunk))
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            break
    return out


def build_ws_url(device_id: str) -> str:
    raw = AK_FPID + AK_APPKEY + device_id + AK_SALT
    access_key = hashlib.md5(raw.encode("utf-8")).hexdigest()
    now = int(time.time())
    params = [
        ("aid", AWEME_AID),
        ("fpid", AK_FPID),
        ("sdk_version", "3"),
        ("device_id", device_id),
        ("iid", device_id),
        ("access_key", access_key),
        ("pl", "0"),
        ("ne", "1"),
        ("version_code", AWEME_VERSION_CODE),
        ("version_name", AWEME_VERSION_NAME),
        ("update_version_code", AWEME_UPDATE_VERSION_CODE),
        ("platform", "0"),
        ("monitor_service_id_list", "[]"),
        ("is_background", "0"),
        ("ping-interval", "30"),
        ("qos_level", "2"),
        ("qos_sdk_version", "2"),
        ("ttnet_ignore_offline", "1"),
        ("ws_connect_protocol", "0"),
        ("device_platform", "android"),
        ("os", "android"),
        ("app_name", AWEME_APP_NAME),
        ("package", AWEME_PACKAGE),
        ("channel", AWEME_CHANNEL),
        ("ac", "wifi"),
        ("language", "zh"),
        ("device_type", AWEME_DEVICE_TYPE),
        ("device_brand", AWEME_DEVICE_BRAND),
        ("os_api", AWEME_OS_API),
        ("os_version", AWEME_OS_VERSION),
        ("ts", str(now)),
        ("_rticket", str(now * 1000)),
    ]
    qs = "&".join(f"{k}={v}" for k, v in params)
    return f"{WS_BASE}?{qs}"


def build_ws_headers(cookie: str) -> dict[str, str]:
    return {
        "User-Agent": AWEME_UA,
        "Origin": "wss://frontier-aweme-lf-ipainner.amemv.com",
        "Cookie": cookie,
        "x-support-qos2": "1",
        "x-support-ack": "1",
        "sdk-version": "2",
        "passport-sdk-version": "601504",
        "X-SS-DP": AWEME_AID,
        "x-tt-store-region": "cn",
        "x-tt-store-region-src": "uid",
        "x-bd-kmsv": "1",
    }


def _iter_msg(top: list, path: list[int]):
    """沿 path 嵌套跳 msg 列表"""
    current = top or []
    for p in path:
        next_level = []
        for item in current:
            if not (isinstance(item, tuple) and len(item) == 3):
                continue
            n, w, v = item
            if n == p and w == 2 and isinstance(v, tuple) and v[0] == "msg":
                next_level.append(v[1])
        if not next_level:
            return
        current = next_level
    yield from current


async def send_text_via_ws(
    cookie_str: str,
    *,
    device_id: str = "",
    conv_id: str,
    conv_type: int = 1,
    short_id: int = 0,
    text: str,
    timeout: float = 8.0,
    cookies: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """通过 frontier WS 发一条文本消息。

    device_id 优先从 cookies 里的 __dy_device_id 伪 cookie 读取（扫码时
    qrlogin.Session 生成并持久化到 cookie JSON），fallback 用入参。
    """
    if cookies:
        for c in cookies:
            if c.get("name") == "__dy_device_id" and c.get("value"):
                device_id = c["value"]
                break
    if not device_id:
        return {"client_msg_id": "", "server_msg_id": "", "blocked": False,
                "reason": "缺少 device_id（请重新扫码让外置服务生成设备指纹）"}
    url = build_ws_url(device_id)
    headers = build_ws_headers(cookie_str)
    frame, cmid = build_ws_send_frame(
        conv_id=conv_id, conv_type=conv_type, short_id=short_id,
        text=text, device_id=device_id,
    )

    out = {"client_msg_id": cmid, "server_msg_id": "", "blocked": False, "reason": ""}
    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            subprotocols=["pbbp2"],
            open_timeout=timeout,
            proxy=None,
            max_size=33_554_432,
        ) as ws:
            await ws.send(frame)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
                except asyncio.TimeoutError:
                    break
                try:
                    top = decode_top(raw)
                except Exception:
                    continue
                for f8 in (item for item in (top or []) if isinstance(item, tuple) and len(item) == 3 and item[0] == 8 and item[1] == 2):
                    _, _, f8_val = f8
                    if not (isinstance(f8_val, tuple) and f8_val[0] == "msg"):
                        continue
                    f8_items = f8_val[1] or []
                    cmd_val = None
                    self_uid_val = None
                    message_id_chars: list[str] = []
                    for n, w, v in f8_items:
                        if n == 1 and w == 0:
                            cmd_val = v
                        elif n == 13 and w == 0:
                            self_uid_val = v
                    if cmd_val != 100:
                        continue
                    if self_uid_val is not None and str(self_uid_val) != str(device_id):
                        continue
                    for n, w, v in f8_items:
                        if n == 7 and w == 2 and isinstance(v, tuple) and v[0] == "msg":
                            for kn, kw, kv in v[1] or []:
                                if kn == 6 and kw == 0:
                                    message_id_chars.append(chr(int(kv)))
                    if message_id_chars:
                        out["server_msg_id"] = "".join(message_id_chars)
                        return out
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"WS error: {type(e).__name__}: {e}"
        return out
    return out
