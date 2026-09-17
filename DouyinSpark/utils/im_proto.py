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
    "TEXT_MESSAGE_TEMPLATE",
    "CREATE_CONVERSATION_TEMPLATE",
    "IM_USER_AGENT",
    "decode_template",
    "encode_send_request",
    "build_text_message_body",
    "build_create_conversation_body",
    "parse_im_response",
    "build_get_by_user_init_body",
    "parse_get_by_user_init_response",
]

# ===== 抓包模板（逐字复制自 im-templates.js） =====
TEXT_MESSAGE_TEMPLATE = 'CGQQvNAFGgUxLjEuMyIxaGFzaC5pNVlMc2lmZnM3MWh6Tko5OGJxcFV1T1o5Uk5QR1NuTVJLS05LU0NEdHM4PSgDMAA6OjhhYTJkY2I6RGV0YWNoZWQ6IDhhYTJkY2I4OGI0MTUzODg4NTE2OGU0YWZiYmQyYjZiYWM4YWVmYjJClQOiBpEDCiAwOjE6ODgyMTQ0NTQyMDg6NDIxMzc4MzU5NjExMDU2NBABGJuEgtbq2JDcZyJLeyJtZW50aW9uX3VzZXJzIjpbXSwiYXdlVHlwZSI6NzAwLCJyaWNoVGV4dEluZm9zIjpbXSwidGV4dCI6IuS6uuW3peWbnuWkjSJ9KhUKEXM6bWVudGlvbmVkX3VzZXJzEgAqOwoTczpjbGllbnRfbWVzc2FnZV9pZBIkNmJkYzFiZGItZWJiMS00ZDNlLWE4MmItZGExMTkzYTA4NDY2Kh0KB3M6c3RpbWUSEjE3NTY4NzU1MzQwNjcuMjIzOTAHOnkxbGFXeHdJT3dSSk93Vm1rT3NwMXY0dEtVRWVDYXp2U0wzdllLMHRoT3RURnBJQ1JuSDllaDh6cElNVDQwd0FDNHltQUhRVWppellRZGdib0dXUDllbnFRZ0FHSVBtM0xnY2JJNDdkdkFDMFZaMlVBcVJKQUExT1hBQiQ2YmRjMWJkYi1lYmIxLTRkM2UtYTgyYi1kYTExOTNhMDg0NjZKATBaCWRvdXlpbl9wY3rkAQoXaWRlbnRpdHlfc2VjdXJpdHlfdG9rZW4SyAF7InRva2VuIjoiQ2poeW5iclB0UG1VcHBhUVI0ektZNEF2LXVYZS1POGJJRnJDWXFmVVJCaW5VVU9fSHljSkNzSXk4YU1HOExac3RfTXRIMEIxajhxUU5ScEtDandBQUFBQUFBQUFBQUFBVDI0VXdlZVFvQmdZanlOR054eUE2U3Vqb0hsVTVKOVppX2VHYml3RkFUYVN0SkU4dnhGYUV3TVBuVXR6VVVUUlVtWVE2Wm43RFJqMnNkRnNJQUlpQVFPbFZqX0EifXoyChtpZGVudGl0eV9zZWN1cml0eV9kZXZpY2VfaWQSEzc1MzgzNjE2NDA2NzA0NjM1MDd6HQoVaWRlbnRpdHlfc2VjdXJpdHlfYWlkEgQ2MzgzehMKC3Nlc3Npb25fYWlkEgQ2MzgzehAKC3Nlc3Npb25fZGlkEgEwehUKCGFwcF9uYW1lEglkb3V5aW5fcGN6FQoPcHJpb3JpdHlfcmVnaW9uEgJjbnqDAQoKdXNlcl9hZ2VudBJ1TW96aWxsYS81LjAgKE1hY2ludG9zaDsgSW50ZWwgTWFjIE9TIFggMTBfMTVfNykgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEzOC4wLjAuMCBTYWZhcmkvNTM3LjM2ehYKDmNvb2tpZV9lbmFibGVkEgR0cnVlehkKEGJyb3dzZXJfbGFuZ3VhZ2USBXpoLUNOehwKEGJyb3dzZXJfcGxhdGZvcm0SCE1hY0ludGVsehcKDGJyb3dzZXJfbmFtZRIHTW96aWxsYXqAAQoPYnJvd3Nlcl92ZXJzaW9uEm01LjAgKE1hY2ludG9zaDsgSW50ZWwgTWFjIE9TIFggMTBfMTVfNykgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEzOC4wLjAuMCBTYWZhcmkvNTM3LjM2ehYKDmJyb3dzZXJfb25saW5lEgR0cnVlehQKDHNjcmVlbl93aWR0aBIEMzQ0MHoVCg1zY3JlZW5faGVpZ2h0EgQxNDQwegsKB3JlZmVyZXISAHoeCg10aW1lem9uZV9uYW1lEg1Bc2lhL1NoYW5naGFpeg0KCGRldmljZUlkEgEwehwKBXdlYmlkEhM3NTM4MzYxNjQwNjcwNDYzNTA3ejoKAmZwEjR2ZXJpZnlfbWViNXdseHdfdWcwdUpOZVdfaEdldl80UW1MXzlraDlfc0J4SENtWTVSMzdZeg0KCGlzLXJldHJ5EgEwkAEEqgEKZG91eWluX3dlYrIBB3dlYl9zZGu6AYUBdHMuMi44MmQyNTUxYmU0ZDJjNDczY2FmNWNjODFkYTdmOTc3ZGRlMjliZjVkOGM0NjdiYTgyMGY2ZTE1NmEyYzg1OGQxYzRmYmU4N2QyMzE5Y2YwNTMxODYyNGNlZGExNDkxMWNhNDA2ZGVkYmViZWRkYjJlMzBmY2U4ZDRmYTAyNTc1ZMIBfGNIVmlMa0pOT0VKeWMzQkxSMHg0V25kV1NuZ3pWR04wVmxCa2FUWkpOMUpaYVU5clFsVm5Sbk55Ym1oMk5qUXlkSHBhU2pSd1NVd3ZkbmxqV0N0RmRrRjRZbmRJZUhaTmR6VlhNVGxZVFUxMVFVbFdNRTh5T0dwblZUMD3KAWBNRVVDSUhDUG1wMENSTFduV0phOXVnSHQveFFQSUVxbzFqQTJXSlQ0WStCc0RXS29BaUVBdnhjR2tSTlFOTStKKzE5THVDOGNEUStOUUtDL1o0VzR0UW9hUTBsZi9aND0='

CREATE_CONVERSATION_TEMPLATE = 'COEEEJtOGgUxLjEuMyIxaGFzaC5GbmNhMEVkK1hpVUI4QVgyUksxMFcyT0lReDk1a0xGTDR1elAxTG04a3VrPSgDMAA6OjhhYTJkY2I6RGV0YWNoZWQ6IDhhYTJkY2I4OGI0MTUzODg4NTE2OGU0YWZiYmQyYjZiYWM4YWVmYjJCFoomEwgBEL3esKm2jOYDEPrpoK+7zAVKATBaCWRvdXlpbl9wY3oTCgtzZXNzaW9uX2FpZBIENjM4M3oQCgtzZXNzaW9uX2RpZBIBMHoVCghhcHBfbmFtZRIJZG91eWluX3BjehUKD3ByaW9yaXR5X3JlZ2lvbhICY256gwEKCnVzZXJfYWdlbnQSdU1vemlsbGEvNS4wIChNYWNpbnRvc2g7IEludGVsIE1hYyBPUyBYIDEwXzE1XzcpIEFwcGxlV2ViS2l0LzUzNy4zNiAoS0hUTUwsIGxpa2UgR2Vja28pIENocm9tZS8xMzkuMC4wLjAgU2FmYXJpLzUzNy4zNnoWCg5jb29raWVfZW5hYmxlZBIEdHJ1ZXoZChBicm93c2VyX2xhbmd1YWdlEgV6aC1DTnocChBicm93c2VyX3BsYXRmb3JtEghNYWNJbnRlbHoXCgxicm93c2VyX25hbWUSB01vemlsbGF6gAEKD2Jyb3dzZXJfdmVyc2lvbhJtNS4wIChNYWNpbnRvc2g7IEludGVsIE1hYyBPUyBYIDEwXzE1XzcpIEFwcGxlV2ViS2l0LzUzNy4zNiAoS0hUTUwsIGxpa2UgR2Vja28pIENocm9tZS8xMzkuMC4wLjAgU2FmYXJpLzUzNy4zNnoWCg5icm93c2VyX29ubGluZRIEdHJ1ZXoUCgxzY3JlZW5fd2lkdGgSBDM0NDB6FQoNc2NyZWVuX2hlaWdodBIEMTQ0MHoLCgdyZWZlcmVyEgB6HgoNdGltZXpvbmVfbmFtZRINQXNpYS9TaGFuZ2hhaXoNCghkZXZpY2VJZBIBMHocCgV3ZWJpZBITNzM2MDk3MTczMTEzMTU2NTU4M3o6CgJmcBI0dmVyaWZ5X21ma3dvdDBpX3FpZnBES2liX1N4eE1fNGJtcV9BS0Q4X2tvUTRFYTRZT3lrd3oNCghpcy1yZXRyeRIBMJABBKoBCmRvdXlpbl93ZWKyAQd3ZWJfc2RrugGFAXRzLjIuNWZjNTU4ZmI5NGFmYjVmNzhmODI2OTQ1NmQwMDQ1NzU3Zjg5ZTVkMDdhMGQ2YWVmYTQ2OTM2NmQxMTJkMWJlOGM0ZmJlODdkMjMxOWNmMDUzMTg2MjRjZWRhMTQ5MTFjYTQwNmRlZGJlYmVkZGIyZTMwZmNlOGQ0ZmEwMjU3NWTCAXxjSFZpTGtKRmIwbDBRMFlyVFZKaFdIRXJhSFZpTDJOVmExWkpaR2N3ZGtwclNtSnZlazFhZG1Wa1RqSlBhbWRuZGtaTGNTdFBUa0pVU2t0T2RtTnlia1paUTI5cGNraEhUMEZyZEM5alIwVkhabmxMSzBGQ1FWUmpkejA9ygFgTUVZQ0lRQ0FtU2FFWkYwTi83VnJrQVlVYTRPWFIxV0JWMlE0WDFSWDFwbkNEYktFcXdJaEFKOGNLNHBkekNoSTVaZ1NoSFJGUkVtRXFKVFQvbnRZMUVrT1A1UGxYdkVF'

# 与 api.py 的 USER_AGENT 保持一致（IM 请求内嵌指纹）
IM_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


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
        (15, "headers", "HeaderField", True),
        (18, "auth_type", "int32", False),
        (21, "biz", "string", False),
        (22, "access", "string", False),
        (23, "ts_sign", "string", False),
        (24, "sdk_cert", "string", False),
        (25, "request_sign", "string", False),
    ),
    "SendMessageBody": _schema(
        (100, "send_message_content", "SendMessageContent", False),
        (609, "create_session_request", "CreateSessionRequest", False),
        (203, "get_by_user_init_query", "GetByUserInitQuery", False),
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
        (1, "status_code", "int32", False),
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
    template_b64: Optional[str] = None,
) -> bytes:
    """构造发送文本消息的 protobuf 请求体（移植 buildTextMessageBody）。"""
    request = decode_template(template_b64 or TEXT_MESSAGE_TEMPLATE)
    body = request.get("send_message_body") or {}
    content = body.get("send_message_content") or {}

    content["conversation_id"] = conversation_id
    content["conversation_short_id"] = int(conversation_short_id)
    content["conversation_type"] = 1
    content["message_type"] = 7
    # 与 JS JSON.stringify({mention_users, aweType, richTextInfos, text}) 一致：
    # 键序固定、无空格、非 ASCII 原样输出
    content["content"] = json.dumps(
        {"mention_users": [], "aweType": 700, "richTextInfos": [], "text": text},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    content["client_message_id"] = client_message_id

    stime = f"{_now_ms()}.{random.randint(0, 9999)}"
    for field in content.get("ext_fields", []):
        if field.get("key") == "s:client_message_id":
            field["value"] = client_message_id
        elif field.get("key") == "s:stime":
            field["value"] = stime

    body["send_message_content"] = content
    request["send_message_body"] = body
    return _encode("DySendMsgRequest", request)


def build_create_conversation_body(
    *,
    receiver_uid: Union[str, int],
    sender_uid: Union[str, int, None] = None,
    template_b64: Optional[str] = None,
) -> bytes:
    """构造创建会话的 protobuf 请求体（移植 buildCreateConversationBody）。"""
    request = decode_template(template_b64 or CREATE_CONVERSATION_TEMPLATE)
    body = request.get("send_message_body") or {}
    create = body.get("create_session_request") or {"session_type": 1}

    if sender_uid:
        create["user"] = [int(receiver_uid), int(sender_uid)]
    else:
        create["user"] = [int(receiver_uid)]
    body["create_session_request"] = create
    request["send_message_body"] = body
    return _encode("DySendMsgRequest", request)


def parse_im_response(data: bytes) -> dict[str, Any]:
    """解析 imapi 响应（message/send 与 conversation/create 共用同一外层结构）。

    返回 {status_message, request_id, self_uid, conversation_id, conversation_short_id, extra_info}
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
