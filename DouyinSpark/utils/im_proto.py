"""抖音 IM protobuf 编解码（手工 wire 实现，不依赖 protobuf 库）。

忠实移植自 douyin-id-spark 的 im-proto.js / im-templates.js：
抓包模板 decode -> patch 业务字段 -> encode；模板中的
token / ts_sign / sdk_cert / request_sign 等长效设备凭据原样保留。

编解码语义对齐 protobufjs：
- decode 只保留 wire 上出现的字段（忽略未知字段）；
- encode 按 proto schema 字段号升序输出，且「只要字段出现就编码」
  （proto3 默认值的 0 / 空串也照写，protobufjs 是 hasOwnProperty 语义）；
- proto3 repeated 数值字段 decode 同时兼容 packed / 非 packed，encode 逐元素非 packed
  （protobufjs 在未显式声明 packed 选项时如此，抓包模板即非 packed）；
- int64 用 Python int（无 JS Number 精度问题，无需 Long 字符串转换）。
"""

from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Optional, Union

__all__ = [
    "IM_USER_AGENT",
    "decode_template",
    "encode_send_request",
    "build_text_message_body",
    "build_create_conversation_body",
    "parse_im_response",
    "build_get_by_user_init_body",
    "parse_get_by_user_init_response",
]

# 与 api.py 的 USER_AGENT 保持一致（IM 请求内嵌指纹）
IM_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# 抖音 IM (imapi) envelope 常量。来自 jumpbyte-bot 真实抓包（PC Electron 抖音 IM），
# 原 douyin-id-spark 模板字段（sdk_version=1.1.3, biz=douyin_web, session_aid=6383）
# 是 web 端抓的，发送会路由到 web 通道导致校验更严。需要切到 PC IM 通道。
WEB_SDK_VERSION = "0.1.8"
WEB_BUILD_NUMBER = "0d50935:feat/pc-im-group"
WEB_SESSION_AID = "339757"
WEB_APP_NAME = "douyin_pc"
WEB_BIZ = "douyin_im_pc"          # 原模板 douyin_web 会按 web 路由校验 → 失败
WEB_ACCESS = "web_sdk"
WEB_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) douyinim/1.1.33 Chrome/136.0.7103.59 "
    "Electron/36.3.2 Safari/537.36"
)
WEB_REFERER = "https://imdesktop.douyin.com"


# ===================== wire 基础读写 =====================


def _write_varint(value: int) -> bytes:
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


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def _skip(buf: bytes, pos: int, wire: int) -> int:
    if wire == 0:
        _, pos = _read_varint(buf, pos)
        return pos
    if wire == 1:
        return pos + 8
    if wire == 2:
        length, pos = _read_varint(buf, pos)
        return pos + length
    if wire == 5:
        return pos + 4
    raise ValueError(f"不支持的 wire type: {wire}")


# ===================== schema（对应 im-proto.js 的 PROTO） =====================


@dataclass(frozen=True)
class _Field:
    name: str
    type: str  # 'int32' | 'int64' | 'string' | 子消息名
    repeated: bool = False


def _schema(*fields: tuple[int, str, str, bool]) -> dict[int, _Field]:
    return {num: _Field(name, typ, rep) for num, name, typ, rep in fields}


_SCHEMAS: dict[str, dict[int, _Field]] = {
    # cmd=100 (send_message) / cmd=609 (create_conversation) / cmd=2006 (conv_list)
    "DySendMsgRequest": _schema(
        (1, "cmd", "int32", False),
        (2, "sequence_id", "int32", False),
        (3, "sdk_version", "string", False),
        (4, "token", "string", False),
        (5, "refer", "int32", False),
        (6, "inbox_type", "int32", False),
        (7, "build_number", "string", False),
        (8, "send_message_body", "SendMessageBody", False),
        (9, "device_id", "string", False),
        (11, "device_platform", "string", False),
        (14, "session_ttl", "string", False),
        (15, "headers", "HeaderField", True),
        (18, "auth_type", "int32", False),
        (21, "biz", "string", False),
        (22, "access", "string", False),
    ),
    "SendMessageBody": _schema(
        (100, "send_message_content", "SendMessageContent", False),
        (609, "create_session_request", "CreateSessionRequest", False),
        (203, "get_by_user_init_query", "GetByUserInitQuery", False),
        (1, "sender", "int64", False),  # web sdk 路径下服务端需要 sender uid
    ),
    "CreateSessionRequest": _schema(
        (1, "session_type", "int32", False),
        (2, "user", "int64", True),
    ),
    "SendMessageContent": _schema(
        (1, "conversation_id", "string", False),
        (2, "conversation_type", "int32", False),
        (3, "conversation_short_id", "int64", False),
        (4, "content", "string", False),
        (5, "ext_fields", "ExtField", True),
        (6, "message_type", "int32", False),
        (7, "ticket", "string", False),
        (8, "client_message_id", "string", False),
    ),
    "ExtField": _schema(
        (1, "key", "string", False),
        (2, "value", "string", False),
    ),
    "HeaderField": _schema(
        (1, "field_name", "string", False),
        (2, "field_value", "string", False),
    ),
    "GetByUserInitQuery": _schema(
        (1, "cursor", "int64", False),
        (2, "count", "int64", False),
    ),
    "DyInitRequest": _schema(
        (1, "cmd", "int32", False),
        (2, "sequence_id", "int32", False),
        (3, "sdk_version", "string", False),
        (4, "token", "string", False),
        (5, "refer", "int32", False),
        (6, "inbox_type", "int32", False),
        (7, "build_number", "string", False),
        (8, "body", "InitBody", False),
        (9, "device_id", "string", False),
        (11, "device_platform", "string", False),
        (14, "session_ttl", "string", False),
        (15, "headers", "HeaderField", True),
        (18, "auth_type", "int32", False),
        (21, "biz", "string", False),
        (22, "access", "string", False),
    ),
    "InitBody": _schema(
        (2043, "query", "InitQuery", False),
    ),
    "InitQuery": _schema(
        (1, "cursor", "int64", False),
        (2, "page_flag", "int64", False),
    ),
    "DyInitResponse": _schema(
        (1, "cmd", "int32", False),
        (2, "sequence_id", "int32", False),
        (3, "error_code", "int32", False),
        (4, "status", "string", False),
        (5, "version", "int32", False),
        (6, "data", "InitPayload", False),
        (7, "request_id", "string", False),
        (10, "timestamp", "int64", False),
        (11, "server_time", "int64", False),
        (13, "user_id", "int64", False),
    ),
    "InitPayload": _schema(
        (2043, "data", "InitData", False),
    ),
    "InitData": _schema(
        (1, "blocks", "ConvBlock", True),
        (2, "has_more", "int64", False),
        (3, "next_cursor", "int64", False),
    ),
    "ConvBlock": _schema(
        (1, "info", "ConvInfo", False),
        (2, "messages", "ConvMessage", True),
    ),
    "ConvMessage": _schema(
        (1, "conversation_id", "string", False),
        (2, "conversation_type", "int32", False),
        (3, "server_message_id", "int64", False),
        (4, "create_time", "int64", False),
        (5, "conversation_short_id", "int64", False),
        (6, "message_type", "int32", False),
        (7, "sender", "int64", False),
        (8, "content", "string", False),
        (10, "client_create_time", "int64", False),
    ),
    "ConvInfo": _schema(
        (1, "conversation_id", "string", False),
        (2, "conversation_short_id", "int64", False),
        (3, "conversation_type", "int32", False),
        (4, "ticket", "string", False),
        (6, "participants", "ParticipantList", False),
        (7, "participants_count", "int32", False),
    ),
    "ParticipantList": _schema(
        (1, "user", "Participant", True),
    ),
    "Participant": _schema(
        (1, "user_id", "int64", False),
        (5, "sec_uid", "string", False),
    ),
    "DySendMsgResponse": _schema(
        (1, "status_code", "int32", False),       # 服务端实际语义：100=已处理但有错；0=完全成功
        (2, "data_size", "int32", False),
        (3, "error_code", "int32", False),
        (4, "status_message", "string", False),
        (5, "extra_status", "int32", False),
        (6, "message_data", "MessageData", False),
        (7, "request_id", "string", False),
        (10, "server_timestamp_1", "int64", False),
        (11, "server_timestamp_2", "int64", False),
        (13, "user_id", "int64", False),
    ),
    "MessageData": _schema(
        (100, "message_info", "MessageInfo", False),
        (609, "create_info", "CreateInfo", False),
    ),
    "CreateInfo": _schema(
        (1, "info", "ConversationInfo", False),
    ),
    "ConversationInfo": _schema(
        (1, "conversation_id", "string", False),
        (2, "conversation_short_id", "int64", False),
    ),
    "MessageInfo": _schema(
        (1, "message_id", "int64", False),
        (3, "message_status", "int32", False),
        (4, "client_message_id", "string", False),
        (5, "message_type", "int32", False),
        (6, "extra_info", "string", False),
    ),
}

_NUMERIC_TYPES = ("int32", "int64")


def _decode(msg_name: str, data: bytes) -> dict[str, Any]:
    fields = _SCHEMAS[msg_name]
    out: dict[str, Any] = {}
    pos = 0
    size = len(data)
    while pos < size:
        tag, pos = _read_varint(data, pos)
        fnum, wire = tag >> 3, tag & 7
        field = fields.get(fnum)
        if field is None:
            pos = _skip(data, pos, wire)
            continue
        if field.type in _NUMERIC_TYPES:
            if field.repeated and wire == 2:
                # proto3 packed
                length, pos = _read_varint(data, pos)
                end = pos + length
                values = []
                while pos < end:
                    v, pos = _read_varint(data, pos)
                    values.append(v)
                out.setdefault(field.name, []).extend(values)
                continue
            value, pos = _read_varint(data, pos)
        elif field.type == "string":
            length, pos = _read_varint(data, pos)
            value = data[pos : pos + length].decode("utf-8")
            pos += length
        else:  # 子消息
            length, pos = _read_varint(data, pos)
            value = _decode(field.type, data[pos : pos + length])
            pos += length
        if field.repeated:
            out.setdefault(field.name, []).append(value)
        else:
            out[field.name] = value
    return out


def _encode_field(msg_name: str, field: _Field, fnum: int, value: Any, out: bytearray) -> None:
    if field.type in _NUMERIC_TYPES:
        out += _write_varint((fnum << 3) | 0)
        out += _write_varint(int(value))
    elif field.type == "string":
        raw = str(value).encode("utf-8")
        out += _write_varint((fnum << 3) | 2)
        out += _write_varint(len(raw))
        out += raw
    else:
        raw = _encode(field.type, value)
        out += _write_varint((fnum << 3) | 2)
        out += _write_varint(len(raw))
        out += raw


def _encode(msg_name: str, obj: dict[str, Any]) -> bytes:
    fields = _SCHEMAS[msg_name]
    out = bytearray()
    # protobufjs 按字段号升序输出；presence 语义：出现即编码（包括 0 / 空串）
    for fnum in sorted(fields):
        field = fields[fnum]
        if field.name not in obj:
            continue
        value = obj[field.name]
        # repeated 逐元素编码（protobufjs 未显式声明 packed 选项时不打包，
        # 与抓包模板和 JS 版 roundtrip 行为一致；decode 端仍兼容 packed）
        if field.repeated:
            for item in value:
                _encode_field(msg_name, field, fnum, item, out)
        else:
            _encode_field(msg_name, field, fnum, value, out)
    return bytes(out)


# ===================== 模板 decode / encode =====================


def decode_template(template_b64: str) -> dict[str, Any]:
    """Base64 抓包模板 -> dict（等价 JS SendMsgRequest.toObject(...)）。"""
    return _decode("DySendMsgRequest", base64.b64decode(template_b64))


def encode_request(msg_name: str, obj: dict[str, Any]) -> bytes:
    return _encode(msg_name, obj)


def encode_send_request(obj: dict[str, Any]) -> bytes:
    return _encode("DySendMsgRequest", obj)


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_text_message_body(
    *,
    conversation_id: str,
    conversation_short_id: Union[str, int],
    text: str,
    client_message_id: str,
    template_b64: Optional[str] = None,  # noqa: ARG001 保留参数兼容旧调用
    device_id: str = "0",
    self_uid: str = "",
) -> bytes:
    """构造发送文本消息的 protobuf 请求体（cmd=100）。

    不再使用模板（模板里的 ts_sign/sdk_cert/request_sign 是抓包瞬间的"证书"，
    服务端复用后会校验失败 → 静默丢弃消息但仍返回 status_code=0 误导客户端）。
    改为从零构造 envelope，与 jumpbyte-bot httpsend.go 一致：
    auth_type=1（普通鉴权）、session_aid=339757、biz=douyin_im_pc。
    """
    content_json = json.dumps(
        {"aweType": 700, "type": 0, "richTextInfos": [], "text": text},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    ms = _now_ms()
    stime = f"{ms}.{random.randint(0, 9999)}"
    send_message_content_dict = {
        "conversation_id": conversation_id,
        "conversation_type": 1,
        "conversation_short_id": int(conversation_short_id),
        "content": content_json,
        "ext_fields": [
            {"key": "s:mentioned_users", "value": ""},
            {"key": "s:client_message_id", "value": client_message_id},
            {"key": "s:stime", "value": stime},
        ],
        "message_type": 7,
        "client_message_id": client_message_id,
    }
    send_message_body_dict: dict = {"send_message_content": send_message_content_dict}
    # sender_uid：web sdk 路径下服务端需要这个字段确定消息发出方
    if self_uid:
        send_message_body_dict["sender"] = int(self_uid)
    request = {
        "cmd": 100,
        "sequence_id": random.randint(10000, 92220),
        "sdk_version": WEB_SDK_VERSION,
        "token": "",  # auth_type=1 不需要证书
        "refer": 3,
        "inbox_type": 0,
        "build_number": WEB_BUILD_NUMBER,
        "send_message_body": send_message_body_dict,
        "device_id": device_id or "0",
        "device_platform": WEB_APP_NAME,
        "session_ttl": "360000",
        "headers": _build_im_headers(device_id or "0"),
        "auth_type": 1,  # 关键：不用模板里的 4 (CERT_AUTH)
        "biz": WEB_BIZ,
        "access": WEB_ACCESS,
    }
    return _encode("DySendMsgRequest", request)


def build_create_conversation_body(
    *,
    receiver_uid: Union[str, int],
    sender_uid: Union[str, int, None] = None,
    template_b64: Optional[str] = None,  # noqa: ARG001
    device_id: str = "0",
    self_uid: str = "",  # noqa: ARG001 兼容旧调用
) -> bytes:
    """构造创建会话的 protobuf 请求体（cmd=609）。

    与 build_text_message_body 同理：从零构造 envelope，不依赖模板。
    """
    users = [int(receiver_uid)]
    if sender_uid:
        users.append(int(sender_uid))
    create_session_dict = {"session_type": 1, "user": users}
    request = {
        "cmd": 609,
        "sequence_id": random.randint(10000, 92220),
        "sdk_version": WEB_SDK_VERSION,
        "token": "",
        "refer": 3,
        "inbox_type": 0,
        "build_number": WEB_BUILD_NUMBER,
        "send_message_body": {"create_session_request": create_session_dict},
        "device_id": device_id or "0",
        "device_platform": WEB_APP_NAME,
        "session_ttl": "360000",
        "headers": _build_im_headers(device_id or "0"),
        "auth_type": 1,
        "biz": WEB_BIZ,
        "access": WEB_ACCESS,
    }
    return _encode("DySendMsgRequest", request)


def parse_im_response(data: bytes) -> dict[str, Any]:
    """解析 imapi 响应（message/send 与 conversation/create 共用同一外层结构）。

    返回 {status_code, error_code, status_message, request_id, self_uid,
          conversation_id, conversation_short_id, extra_info}
    （int64 字段为 Python int，对应 JS 版 longs:String 的字符串值）。
    """
    response = _decode("DySendMsgResponse", data)
    extra_info: Any = None
    message_data = response.get("message_data") or {}
    message_info = message_data.get("message_info") or {}
    raw_extra = message_info.get("extra_info")
    if raw_extra:
        try:
            extra_info = json.loads(raw_extra)
        except (json.JSONDecodeError, TypeError):
            extra_info = {"raw": raw_extra}
    create_info = (message_data.get("create_info") or {}).get("info") or {}
    return {
        "status_code": response.get("status_code", 0),
        "error_code": response.get("error_code", 0),
        "data_size": response.get("data_size", 0),
        "status_message": response.get("status_message", ""),
        "request_id": response.get("request_id", ""),
        "self_uid": response.get("user_id", 0),
        "conversation_id": create_info.get("conversation_id", ""),
        "conversation_short_id": create_info.get("conversation_short_id", 0),
        "extra_info": extra_info,
    }


def _build_init_headers() -> list[dict[str, str]]:
    """与抓包一致的 header 指纹（user_agent 与 HTTP 头、签名 UA 保持一致）。"""
    pairs = [
        ("session_aid", "6383"),
        ("session_did", "0"),
        ("app_name", "douyin_pc"),
        ("priority_region", "cn"),
        ("user_agent", IM_USER_AGENT),
        ("cookie_enabled", "true"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "MacIntel"),
        ("browser_name", "Mozilla"),
        (
            "browser_version",
            "5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        ),
        ("browser_online", "true"),
        ("screen_width", "3440"),
        ("screen_height", "1440"),
        ("referer", ""),
        ("timezone_name", "Asia/Shanghai"),
        ("deviceId", "0"),
        ("is-retry", "0"),
    ]
    return [{"field_name": k, "field_value": v} for k, v in pairs]


def _build_im_headers(device_id: str = "0") -> list[dict[str, str]]:
    """发消息 / 创建会话用的 f15 指纹。

    与 jumpbyte-bot 一致：session_aid=339757 (PC IM)、UA 用 Electron douyinim、
    平台 Win32（与 imdesktop UA 对齐）。
    """
    ua = WEB_PC_UA
    browser_version = ua.replace("Mozilla/", "")
    pairs = [
        ("session_aid", WEB_SESSION_AID),
        ("session_did", device_id or "0"),
        ("app_name", WEB_APP_NAME),
        ("priority_region", "cn"),
        ("user_agent", ua),
        ("cookie_enabled", "true"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"),
        ("browser_name", "Mozilla"),
        ("browser_version", browser_version),
        ("browser_online", "true"),
        ("screen_width", "1707"),
        ("screen_height", "1067"),
        ("referer", ""),
        ("timezone_name", "Asia/Shanghai"),
        ("is-retry", "0"),
    ]
    return [{"field_name": k, "field_value": v} for k, v in pairs]


def build_get_by_user_init_body(
    *,
    cursor: Union[str, int] = "0",
    sequence_id: int = 10001,
) -> bytes:
    """构造拉取会话列表（get_message_by_init）的 protobuf 请求体（从零构造，不依赖模板）。"""
    is_first_page = not cursor or str(cursor) == "0"
    request = {
        "cmd": 2043,
        "sequence_id": int(sequence_id),
        "sdk_version": "0.1.8",
        "token": "",
        "refer": 3,
        "inbox_type": 1,
        "build_number": "0d50935:feat/pc-im-group",
        "body": {
            "query": {"page_flag": 0} if is_first_page else {"cursor": int(cursor), "page_flag": 1}
        },
        "device_id": "0",
        "device_platform": "douyin_pc",
        "session_ttl": "360000",
        "headers": _build_init_headers(),
        "auth_type": 1,
        "biz": "douyin_web",
        "access": "web_sdk",
    }
    return _encode("DyInitRequest", request)


def parse_get_by_user_init_response(data: bytes) -> dict[str, Any]:
    """解析 get_message_by_init 响应（移植 parseGetByUserInitResponse）。

    返回 {status, error_code, self_uid, has_more, next_cursor, conversations}；
    会话项 {conversation_id, conversation_short_id, conversation_type, ticket,
    participants_count, participants[{uid, sec_uid}], messages[{sender, client_create_time, message_type}]}。
    """
    response = _decode("DyInitResponse", data)
    status = response.get("status", "")
    if status != "OK":
        raise ValueError(status or f"状态码 {response.get('error_code', '未知')}")
    init_data = (response.get("data") or {}).get("data") or {}
    conversations = []
    for block in init_data.get("blocks", []):
        info = block.get("info")
        if not info or not info.get("conversation_id"):
            continue
        conversations.append(
            {
                "conversation_id": str(info["conversation_id"]),
                "conversation_short_id": info.get("conversation_short_id", 0),
                "conversation_type": info.get("conversation_type", 0),
                "ticket": str(info.get("ticket", "")),
                "participants_count": info.get("participants_count", 0),
                "participants": [
                    {"uid": user.get("user_id", 0), "sec_uid": str(user.get("sec_uid", ""))}
                    for user in (info.get("participants") or {}).get("user", [])
                ],
                # 该会话最近消息（用于「今天已续过」判断），按时间升序返回
                "messages": [
                    {
                        "sender": message.get("sender", 0),
                        "client_create_time": message.get("client_create_time", 0),
                        "message_type": message.get("message_type", 0),
                    }
                    for message in block.get("messages", [])
                ],
            }
        )
    return {
        "status": status,
        "error_code": response.get("error_code", 0),
        "self_uid": response.get("user_id", 0),
        "has_more": init_data.get("has_more", 0) == 1,
        "next_cursor": init_data.get("next_cursor", 0),
        "conversations": conversations,
    }
