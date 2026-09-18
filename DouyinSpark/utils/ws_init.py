"""抖音 IM 设备指纹注册（WebSocket init）。

为什么需要：imapi.douyin.com HTTP 通道要求 device_id + access_key + install_id 等
"前端已经注册过"的指纹，否则直接返回 StatusCode_130（"用户未在该通道注册设备"）。

解决方案：连一次 frontier-aweme WS（jumpbyte-bot 的连接端点），让服务端注册设备。
之后 HTTP imapi 调用就会被识别为同一设备，可以正常发消息。

注：不需要在 WS 上收发消息，只建立一次连接即可。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Optional

import websockets

# 来自 jumpbyte-bot internal/engine/client.go
# 安卓 frontier 通道：wss://frontier-aweme-lf-ipainner.amemv.com/ws/v2
WS_BASE = "wss://frontier-aweme-lf-ipainner.amemv.com/ws/v2"

# 安卓 IM SDK 参数
AK_FPID = "9"
AK_APPKEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
AK_SALT = "f8a69f1719916z"
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

# 进程级缓存：账号 ID → init 是否完成
_INIT_CACHE: dict[str, float] = {}
_INIT_TTL = 3600.0  # 1 小时


def compute_access_key(device_id: str) -> str:
    """md5(fpid + appkey + device_id + salt)"""
    raw = AK_FPID + AK_APPKEY + device_id + AK_SALT
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_ws_url(device_id: str) -> str:
    now = int(time.time())
    params: list[tuple[str, str]] = [
        ("aid", AWEME_AID),
        ("fpid", AK_FPID),
        ("sdk_version", "3"),
        ("device_id", device_id),
        ("iid", device_id),
        ("access_key", compute_access_key(device_id)),
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
        "Sec-WebSocket-Protocol": "pbbp2",
    }


async def ensure_device_registered(cookie: str, device_id: str, timeout: float = 10.0) -> bool:
    """连一次 frontier WS 完成设备注册。返回 True 表示成功。

    同一 device_id 在 _INIT_TTL 秒内只连一次（缓存）。
    """
    if not device_id or device_id == "0":
        return False

    now = time.time()
    last = _INIT_CACHE.get(device_id)
    if last and now - last < _INIT_TTL:
        return True

    url = build_ws_url(device_id)
    headers = build_ws_headers(cookie)
    # 服务端强制 Sec-WebSocket-Protocol: pbbp2；websockets 库要把 pbbp2 当 subprotocol 传
    # 同时 addtional_headers 不要重复带 Sec-WebSocket-Protocol（让库处理）
    subprotocols = ["pbbp2"]
    try:
        async with websockets.connect(
            url,
            additional_headers={k: v for k, v in headers.items() if k.lower() != "sec-websocket-protocol"},
            subprotocols=subprotocols,
            open_timeout=timeout,
            proxy=None,
        ) as ws:
            # 服务端 accept 后会发一帧初始 ack。等 0.5s 让服务端注册完成，然后关闭。
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                # 没收到数据也 OK——服务端可能主动注册了
                pass
        _INIT_CACHE[device_id] = now
        return True
    except Exception as e:  # noqa: BLE001
        # 连接失败也别抛错——返回 False 让上层决定要不要重试
        import logging
        logging.getLogger(__name__).warning(
            f"[DouyinSpark] WS init 失败 device_id={device_id}: {e}"
        )
        return False
