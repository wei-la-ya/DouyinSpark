"""抖音扫码登录（纯 API，无浏览器，异步版）。

忠实移植自 douyin-id-spark 的 qr-login.js（其本身移植自 jumpbyte-bot 的
抖音 PC 客户端登录流程）：走 imdesktop.douyin.com（PC 端 passport），
签名 sign/qs 规则与 account_sdk_source_info 指纹见下。

用法：
    session = QrLoginSession()
    await session.start()          # 开始，后台轮询
    session.qr                     # data:image/png;base64,... 二维码
    session.status                 # waiting | scanned | sms | success | error
    session.submit_sms_code(code)  # 触发短信验证时提交验证码
    session.cookies                # 成功后的 Cookie-Editor 格式数组
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .abogus import sign_abogus

HOST = "https://imdesktop.douyin.com"
AID = "339757"
APP_KEY = "3c452fb664e3de0e936108429a0bc697"
NEXT_URL = "https://www.douyin.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "douyinim/1.1.31 Chrome/130.0.6723.58 Electron/33.4.11 Safari/537.36"
)
REQUEST_TIMEOUT = 30.0  # 秒（JS REQUEST_TIMEOUT_MS = 30000）

# 浏览器发包时的固定参数顺序（取自真机 HAR；顺序乱了对不上风控）
PARAM_ORDER = {
    "passport_jssdk_version": 0, "passport_jssdk_type": 1, "is_from_ttaccountsdk": 2,
    "aid": 3, "language": 4, "account_app_language": 5, "ts": 6,
    "next": 7, "need_logo": 8, "need_short_url": 9, "is_new_login": 10,
    "is_from_iesaccountsaas": 11, "account_sdk_source": 12, "account_sdk_source_info": 13,
    "p_js_v": 14, "p_js_t": 15, "p_zt": 16, "p_ver": 17, "request_host": 18, "p_bd": 19,
    "biz_trace_id": 20, "new_authn_sdk_version": 21,
    "device_id": 22, "iid": 23, "version_code": 24, "device_platform": 25,
    "sign": 100, "qs": 101, "msToken": 102, "a_bogus": 103,
}

__all__ = ["xor5", "QrLoginSession", "CookieJar", "PARAM_ORDER"]


def _encode_uri_component(value: str) -> str:
    """JS encodeURIComponent 等价物。"""
    return quote(value, safe="-_.!~*'()")


def xor5(input_value: Any) -> str:
    """code_encrypt / account_sdk_source_info 编码：UTF-8 字节逐位 ^ 5 后转十六进制。"""
    out = []
    for ch in str(input_value):
        c = ord(ch)
        if c <= 0x7F:
            out.append(c)
        elif c <= 0x7FF:
            out += [0xC0 | ((c >> 6) & 0x1F), 0x80 | (c & 0x3F)]
        elif c <= 0xFFFF:
            out += [0xE0 | ((c >> 12) & 0x0F), 0x80 | ((c >> 6) & 0x3F), 0x80 | (c & 0x3F)]
        # > 0xffff 跳过，与 JS 原版一致
    return "".join(f"{b ^ 5:02x}" for b in out)


def _sorted_kv(map_: dict[str, str], limit: int) -> tuple[str, list[str]]:
    """排序后前 limit 个参数拼 k=v&k=v；limit<0 为全部。返回 (拼接串, 使用的键)。"""
    keys = sorted(map_.keys())
    if 0 <= limit < len(keys):
        keys = keys[:limit]
    return "&".join(f"{k}={map_[k]}" for k in keys), keys


def sign_params(query: dict[str, str], body: Optional[dict[str, str]]) -> dict[str, str]:
    """sign = sha256(前10个排序query + "&" + 排序body + "&app_key=...")；qs = xor5(前10个键名)。"""
    t_str, keys = _sorted_kv(query, 10)
    e_str, _ = _sorted_kv(body or {}, -1)
    sign = hashlib.sha256(f"{t_str}&{e_str}&app_key={APP_KEY}".encode("utf-8")).hexdigest()
    return {"sign": sign, "qs": xor5(",".join(keys))}


_MS_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _ms_token(length: int = 128) -> str:
    return "".join(_MS_ALPHABET[b & 63] for b in os.urandom(length))


def _rand_hex(length: int) -> str:
    return os.urandom((length + 1) // 2).hex()[:length]


def _gen_device_id() -> str:
    """生成 device_id：324 开头 + 7 位随机数字。"""
    return "324" + "".join(str(b % 10) for b in os.urandom(7))


def _build_fingerprint(device_id: str) -> str:
    """account_sdk_source_info 指纹（xor5(JSON)）。

    内容为稳定合理的 Windows+Chromium 指纹；服务端只要能解码即可。
    """
    payload = {
        "hardwareConcurrency": 8,
        "webdriver": False,
        "chromedriver": False,
        "shelldriver": False,
        "plugins": 5,
        "permissions": [{"name": "notifications", "state": "granted"}],
        "innerHeight": 484,
        "innerWidth": 726,
        "outerHeight": 484,
        "outerWidth": 726,
        "stoargeStatus": {
            "indexedDB": {
                "idb": "object", "open": "function", "indexedDB": "object",
                "IDBKeyRange": "function", "openDatabase": "function",
                "isSafari": False, "hasFetch": False,
            },
            "localStorage": {"isSupportLStorage": True, "size": 1993, "write": True},
            "storageQuotaStatus": {"usage": 0, "quota": 36104626176, "isPrivate": False},
        },
        "webgl": {
            "vendor": "Google Inc. (Google)",
            "renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)",
        },
        "notificationPermission": "granted",
        "performance": {
            "timeOrigin": 1787813991280.3,
            "usedJSHeapSize": 18200000,
            "navigationTiming": {
                "decodedBodySize": 2527, "entryType": "navigation",
                "initiatorType": "navigation",
                "name": f"file:///renderer/login/index.html?window=login&channel=0&guid={device_id}",
                "renderBlockingStatus": "non-blocking",
            },
        },
        "request_host": "",
        "request_pathname": "/renderer/login/index.html",
        "browser": {"t": "7781993187871", "bit_protocol": "false", "bit_helper": False},
    }
    # 与 JS JSON.stringify 一致：键序按源码书写顺序、无空格
    return xor5(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def encode_kv(map_: dict[str, str]) -> str:
    keys = sorted(
        map_.keys(),
        key=lambda k: (0, PARAM_ORDER[k]) if k in PARAM_ORDER else (1, k),
    )
    return "&".join(f"{k}={_encode_uri_component(str(map_[k]))}" for k in keys)


class CookieJar:
    """极简 Cookie Jar（移植自 JS CookieJar）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def update(self, set_cookies: Optional[list[str]]) -> None:
        for line in set_cookies or []:
            pair = str(line).split(";", 1)[0]
            index = pair.find("=")
            if index <= 0:
                continue
            name = pair[:index].strip()
            value = pair[index + 1 :].strip()
            if not value or value.lower() == "deleted":
                self.store.pop(name, None)
            else:
                self.store[name] = value

    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.store.items())

    def has(self, name: str) -> bool:
        return name in self.store


def _pick(map_: Optional[dict[str, Any]], key: str, default: Optional[str] = None) -> str:
    value = (map_ or {}).get(key)
    return str(value if value is not None else (default if default is not None else ""))


def _pick_biz_params(bp: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key in (
        "passport_mfa_retry_tag", "std_verify_flow_id", "std_verify_scene",
        "std_verify_template", "std_verify_token", "std_verify_type", "std_verify_way",
    ):
        if key in bp:
            out[key] = str(bp[key])
    return out


class QrLoginSession:
    """扫码登录会话（状态体现在 status 属性）。"""

    def __init__(self) -> None:
        self.device_id = _gen_device_id()
        self.fingerprint = _build_fingerprint(self.device_id)
        self.jar = CookieJar()
        self.status = "idle"
        self.qr = ""
        self.screen_name = ""
        self.message = ""
        self.cookies: Optional[list[dict[str, str]]] = None
        self.error: Optional[str] = None
        self.cancelled = False
        self.token = ""
        self.expire_at = 0.0
        self._sms_future: Optional[asyncio.Future[str]] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task[None]] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def cancel(self) -> None:
        self.cancelled = True
        if self._sms_future is not None and not self._sms_future.done():
            self._sms_future.set_result("")
        self._sms_future = None

    def submit_sms_code(self, code: str) -> None:
        if self._sms_future is not None and not self._sms_future.done():
            self._sms_future.set_result(code)
        self._sms_future = None

    async def _wait_for_sms(self) -> str:
        self._sms_future = asyncio.get_running_loop().create_future()
        return await self._sms_future

    def _normal_base(self) -> dict[str, str]:
        return {
            "passport_jssdk_version": "2.4.12", "passport_jssdk_type": "normal",
            "is_from_ttaccountsdk": "1",
            "aid": AID, "language": "zh", "ts": str(int(time.time())),
            "account_sdk_source": "web", "account_sdk_source_info": self.fingerprint,
            "p_js_v": "2.4.12", "p_js_t": "pro", "p_zt": "3.3.5", "p_ver": "1.0.29",
            "request_host": "file://", "p_bd": "1.0.1.7", "biz_trace_id": _rand_hex(8),
            "device_id": self.device_id, "iid": "0", "version_code": "1.1.31",
            "device_platform": "PC",
            "is_from_iesaccountsaas": "1", "is_new_login": "1",
        }

    def _lite_base(self) -> dict[str, str]:
        return {
            "passport_jssdk_version": "5.1.2", "passport_jssdk_type": "lite",
            "is_from_ttaccountsdk": "1",
            "aid": AID, "language": "zh", "account_app_language": "zh",
            "new_authn_sdk_version": "1.0.0.421-web",
            "biz_trace_id": _rand_hex(8), "device_id": self.device_id, "iid": "0",
            "version_code": "1.1.31",
            "device_platform": "PC", "is_from_iesaccountsaas": "1", "is_new_login": "1",
        }

    async def call(
        self,
        path: str,
        query_extra: Optional[dict[str, str]] = None,
        body: Optional[dict[str, str]] = None,
        lite: bool = False,
    ) -> dict[str, Any]:
        """发一个已签名的 passport 请求，返回解析后的 JSON。"""
        query = {**(self._lite_base() if lite else self._normal_base()), **(query_extra or {})}
        if not lite:
            query.update(sign_params(query, body))
        query["msToken"] = _ms_token(128)
        query_str = encode_kv(query)
        a_bogus = sign_abogus(query_str, UA)
        url = f"{HOST}{path}?{query_str}&a_bogus={_encode_uri_component(a_bogus)}"

        headers = {
            "bd-ticket-guard-version": "2",
            "bd-ticket-guard-iteration-version": "2",
            "bd-ticket-guard-ree-public-key": "BEPhQJtcnGrFIlCf8/m+Boe2kyBwe7Wj0hKUVpdDZlj1Dbb4qkcqtSzxGD4eaO6mc4aG9alH1Ka95D1e1ngTKJg=",
            "bd-ticket-guard-server-cert-sn": "533240336124694022040808462028007165443034493949",
            "x-tt-passport-aid-sign": "437536ae85fd28413d036ecf7bf60798421979bdc1fcc15a493474d3bacfb525",
            "x-tt-passport-csrf-token": "",
            "x-tt-passport-trace-id": _rand_hex(8),
            "x-tt-passport-verify-portrait": "41918735-2cb8-47d6-a412-9f970bb8410d.login",
            "user-agent": UA,
            "referer": HOST,
            "accept": "application/json, text/plain, */*",
        }
        cookie_header = self.jar.header()
        if cookie_header:
            headers["cookie"] = cookie_header
        if body:
            headers["content-type"] = "application/x-www-form-urlencoded"

        http = self._http()
        if body:
            response = await http.post(url, headers=headers, content=encode_kv(body))
        else:
            response = await http.get(url, headers=headers)
        self.jar.update(response.headers.get_list("set-cookie"))
        text = response.text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"_raw": text[:200], "_status": response.status_code}

    async def ttwid_check(self) -> None:
        """登录前置：拿设备追踪 cookie（探测失败不阻断登录）。"""
        try:
            response = await self._http().post(
                f"{HOST}/ttwid/check/",
                headers={
                    "user-agent": UA,
                    "content-type": "application/json",
                    "referer": HOST,
                },
                content=json.dumps(
                    {
                        "aid": 339757, "service": "imdesktop.douyin.com",
                        "unionHost": "https://ttwid.bytedance.com",
                        "host": "https://imdesktop.douyin.com", "union": False,
                        "needFid": False, "fid": "", "migrate_priority": 0,
                    },
                    separators=(",", ":"),
                ),
            )
            self.jar.update(response.headers.get_list("set-cookie"))
        except httpx.HTTPError:
            pass  # 探测失败不阻断登录

    async def start(self) -> None:
        """开始登录流程（异步后台轮询，状态体现在 self.status）。"""
        if self.status != "idle":
            return
        self.status = "waiting"
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self.ttwid_check()
            qr = await self.call(
                "/passport/web/get_qrcode/",
                {"next": NEXT_URL, "need_logo": "false", "need_short_url": "false"},
            )
            data = (qr or {}).get("data") or {}
            error_code = data.get("error_code")
            if not data.get("qrcode") or int(error_code if error_code is not None else -1) != 0:
                raise RuntimeError(
                    f"获取二维码失败：{data.get('description') or (qr or {}).get('message') or json.dumps(qr)[:200]}"
                )
            self.qr = f"data:image/png;base64,{data['qrcode']}"
            self.token = str(data["token"])
            self.expire_at = (
                float(data["expire_time"]) * 1000
                if float(data.get("expire_time") or 0)
                else time.time() * 1000 + 180000
            )
            await self.run_loop()
        except Exception as error:
            self.status = "error"
            self.error = str(error) or "扫码登录失败"

    async def run_loop(self) -> None:
        """轮询扫码状态（调用前需已设置 self.token / self.expire_at）。"""
        token = self.token
        expire_at = self.expire_at or time.time() * 1000 + 180000
        base_body = {
            "need_logo": "false", "need_short_url": "false", "is_frontier": "true",
            "token": token, "is_new_login": "1", "next": NEXT_URL,
        }
        extra_body: dict[str, str] = {}
        scanned = False
        mfa_done = False

        while time.time() * 1000 < expire_at and not self.cancelled:
            body = {**base_body, **extra_body}
            try:
                result = await self.call("/passport/web/check_qrconnect/", None, body)
            except httpx.HTTPError:
                await asyncio.sleep(2)
                continue
            d = (result or {}).get("data") or {}

            if not mfa_done and (d.get("account_flow") == "verify" or d.get("biz_params")):
                await self._do_mfa(d)
                extra_body = _pick_biz_params(d.get("biz_params") or {})
                mfa_done = True
                continue

            if d.get("status") == "scanned":
                if not scanned:
                    scanned = True
                    self.screen_name = str((d.get("scan_user_info") or {}).get("screen_name") or "")
                    self.status = "scanned"
                    self.message = (
                        f"{self.screen_name} 已扫码，请在抖音 App 上确认"
                        if self.screen_name
                        else "已扫码，请在抖音 App 上确认"
                    )
            elif d.get("status") == "confirmed":
                if not self.jar.has("sessionid"):
                    raise RuntimeError("已确认但未拿到 sessionid，请重试")
                self.cookies = [
                    {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
                    for name, value in self.jar.store.items()
                ]
                self.status = "success"
                self.message = "登录成功"
                return
            elif int(d.get("error_code") or 0) == 4031:
                raise RuntimeError("触发抖音风控（4031），请稍后重试或改用粘贴 Cookie 方式")
            await asyncio.sleep(2)
        if not self.cancelled:
            raise RuntimeError("二维码已过期，请刷新后重新扫码")

    async def _do_mfa(self, d: dict[str, Any]) -> None:
        """短信二次验证（MFA）。"""
        bp = d.get("biz_params") or {}
        cp = d.get("common_params") or {}
        mfa = {
            "mix_mode": "1", "type": "3737", "encrypt_uid": _pick(d, "encrypt_uid"),
            "verify_ticket": "",
            "copywriting_key": _pick(cp, "copywriting_key", "qr_connect"),
            "ies_safety_diversion_tag": _pick(cp, "ies_safety_diversion_tag", "mfa"),
            "new_verify_flow": _pick(cp, "new_verify_flow"),
            "std_verify_flow_id": _pick(bp, "std_verify_flow_id", _pick(cp, "std_verify_flow_id")),
            "std_verify_scene": _pick(bp, "std_verify_scene", "account_login"),
            "std_verify_template": _pick(bp, "std_verify_template", "ato"),
            "std_verify_token": _pick(bp, "std_verify_token", _pick(cp, "std_verify_token")),
            "std_verify_type": _pick(bp, "std_verify_type", "MFA"),
            "std_verify_way": "mobile_sms_verify",
        }

        def with_tail(extra: dict[str, str]) -> dict[str, str]:
            return {**mfa, **extra, "aid": "339757", "new_authn_sdk_version": "1.0.0.421-web"}

        sent = await self.call(
            "/passport/web/send_code/", None, with_tail({"is6Digits": "1"}), lite=True
        )
        mobile = str(((sent or {}).get("data") or {}).get("mobile") or "")
        self.status = "sms"
        self.message = f"已向 {mobile or '绑定手机'} 发送短信验证码，请输入"
        code = (await self._wait_for_sms()).strip()
        if not code.isdigit() or not 4 <= len(code) <= 8:
            raise RuntimeError("短信验证码无效")
        self.message = "正在校验短信验证码…"
        validated = await self.call(
            "/passport/web/validate_code/", None, with_tail({"code": xor5(code)}), lite=True
        )
        if not ((validated or {}).get("data") or {}).get("ticket"):
            raise RuntimeError(
                f"短信验证失败：{(validated or {}).get('data', {}).get('description') or (validated or {}).get('message') or '未知错误'}"
            )
        self.status = "waiting"
        self.message = "短信验证通过，请继续扫码确认"
