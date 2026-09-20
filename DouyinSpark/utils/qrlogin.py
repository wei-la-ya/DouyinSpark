"""抖音扫码登录（纯 API，无浏览器，异步版）。

走 imdesktop.douyin.com（PC 端 passport），签名 sign/qs 规则与 account_sdk_source_info 指纹见下。

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
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .abogus import sign_abogus  # noqa: F401  # 兼容外部可能的依赖
from .abogus_imdesktop import sign_a_bogus

HOST = "https://imdesktop.douyin.com"
AID = "339757"
APP_KEY = "3c452fb664e3de0e936108429a0bc697"
NEXT_URL = "https://www.douyin.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "douyinim/1.1.31 Chrome/130.0.6723.58 Electron/33.4.11 Safari/537.36"
)
REQUEST_TIMEOUT = 30.0

# passport 接口参数顺序固定（顺序乱了对不上风控）
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
    """极简 Cookie Jar。"""

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
        """发一个已签名的 passport 请求，返回解析后的 JSON。

        a_bogus 用 imdesktop 端 dhzx 变体（与 web 端 cus 变体算法不同）：
        web 端的 sign_abogus 用于 douyin.com；imdesktop passport 必须用 dhzx。
        """
        query = {**(self._lite_base() if lite else self._normal_base()), **(query_extra or {})}
        if not lite:
            query.update(sign_params(query, body))
        query["msToken"] = _ms_token(128)
        query_str = encode_kv(query)
        body_str = encode_kv(body) if body else ""
        a_bogus = sign_a_bogus(query_str, body_str, UA, int(time.time() * 1000))
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
            response = await http.post(url, headers=headers, content=body_str)
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
        """轮询扫码状态（调用前需已设置 self.token / self.expire_at）。

        限流策略：
          - 正常 3s 一次，命中限流后指数退避 3s→6s→12s→15s（封顶）
          - 持续限流 90 秒未缓解则放弃，提示用户挂代理或等待
          - 多种限流描述同时识别（error_code=7 / 太频繁 / 频繁操作 / 操作频繁）
          - 网络异常 15 次、异常响应 8 次上限避免死循环
          - 确认状态多分支兼容（confirmed / confirm / success / complete）
        """
        token = self.token
        expire_at = self.expire_at or time.time() * 1000 + 180000
        base_body = {
            "need_logo": "false", "need_short_url": "false", "is_frontier": "true",
            "token": token, "is_new_login": "1", "next": NEXT_URL,
        }
        extra_body: dict[str, str] = {}
        scanned = False
        mfa_done = False
        fail_count = 0
        rate_limit_since: float = 0.0
        poll_ms = 3000
        last_status = ""

        while time.time() * 1000 < expire_at and not self.cancelled:
            body = {**base_body, **extra_body}
            try:
                result = await self.call("/passport/web/check_qrconnect/", None, body)
            except httpx.HTTPError as err:
                fail_count += 1
                self.message = f"扫码轮询网络异常（{fail_count}/15）：{err.__class__.__name__}"
                if fail_count >= 15:
                    raise RuntimeError(
                        "检查扫码状态连续网络失败，请检查网络后重试（也可能是 IP 被风控）"
                    )
                await asyncio.sleep(poll_ms / 1000)
                continue
            d = (result or {}).get("data") or {}

            if not mfa_done and (d.get("account_flow") == "verify" or d.get("biz_params")):
                fail_count = 0
                rate_limit_since = 0.0
                await self._do_mfa(d)
                extra_body = _pick_biz_params(d.get("biz_params") or {})
                mfa_done = True
                continue

            status = str(d.get("status") or "")
            if not status:
                # 无状态响应：可能是限流（error_code=7）、WAF 拦截或协议变化
                desc = (
                    str((result or {}).get("message") or "")
                    + " "
                    + str(d.get("description") or "")
                ).strip()[:200]
                is_rate_limit = (
                    int(d.get("error_code") or 0) == 7
                    or bool(re.search(r"太频繁|频繁操作|操作频繁", desc))
                )
                if is_rate_limit:
                    if not rate_limit_since:
                        rate_limit_since = time.time()
                    elapsed = int(time.time() - rate_limit_since)
                    self.message = (
                        f"命中抖音限流，将在 {poll_ms // 1000}s 后重试（已持续 {elapsed}s）"
                    )
                    if elapsed > 90:
                        raise RuntimeError(
                            "抖音持续限流（90 秒未缓解），扫码与手机号登录均不可用。\n"
                            "本服务使用合成指纹，抖音把同一指纹下的所有登录方式都限流了，\n"
                            "但同一 IP 下真实浏览器不受影响（指纹不是机器人）。\n"
                            "请用真实浏览器打开 douyin.com 登录 → 用 Cookie-Editor 导出\n"
                            "Cookie JSON → 粘到本页「Cookie JSON」文本框提交。\n"
                            "（Cookie-Editor 用法：浏览器装插件 → 打开 douyin.com →\n"
                            "插件里点 Export → 选 Header format 复制 JSON）"
                        )
                    poll_ms = min(poll_ms * 2, 15000)
                    await asyncio.sleep(poll_ms / 1000)
                    continue
                # 非限流的异常响应：累加失败计数
                rate_limit_since = 0.0
                fail_count += 1
                self.message = f"扫码状态异常（{fail_count}/8）：{desc or '(空响应)'}"
                if fail_count >= 8:
                    raise RuntimeError(
                        f"检查扫码状态连续失败：{desc or '(空响应)'}（可能被限流，请稍后再试）"
                    )
                await asyncio.sleep(poll_ms / 1000)
                continue

            # 有状态：清零限流与失败计数，恢复 3s 节奏
            fail_count = 0
            rate_limit_since = 0.0
            poll_ms = 3000

            if status != last_status:
                last_status = status
                if status == "scanned":
                    self.message = "已扫码，请在抖音 App 上点击【确认登录】"
                elif status == "expired":
                    self.message = "二维码已过期，请刷新"
                else:
                    self.message = f"等待扫码确认…（状态：{status}）"

            if status == "scanned":
                if not scanned:
                    scanned = True
                    self.screen_name = str(
                        (d.get("scan_user_info") or {}).get("screen_name") or ""
                    )
                    self.status = "scanned"
                    self.message = (
                        f"{self.screen_name} 已扫码，请在抖音 App 上确认"
                        if self.screen_name
                        else "已扫码，请在抖音 App 上确认"
                    )
            elif status in ("confirmed", "confirm", "success", "complete"):
                if not self.jar.has("sessionid"):
                    raise RuntimeError("已确认但未拿到 sessionid，请重试")
                # 调 /aweme/v1/web/user/profile/self/ 拿当前最新昵称（不是登录会话里的旧缓存）
                # 注：/passport/account/info/v2/ 返回的是登录会话建立那一刻的快照，
                # 用户改过昵称后接口仍返回旧值，必须用 profile/self 接口
                name = ""
                try:
                    info = await self.call("/aweme/v1/web/user/profile/self/", None, None)
                    user_data = (info or {}).get("user") or {}
                    if user_data.get("nickname"):
                        name = str(user_data["nickname"])
                except (httpx.HTTPError, RuntimeError):
                    # fallback：回落到 user_data
                    ud = d.get("user_data") or {}
                    name = str(ud.get("screen_name") or ud.get("name") or "")
                self.cookies = [
                    {"name": n, "value": value, "domain": ".douyin.com", "path": "/"}
                    for n, value in self.jar.store.items()
                ]
                self.screen_name = name
                self.status = "success"
                self.message = "登录成功"
                return
            elif int(d.get("error_code") or 0) == 4031:
                raise RuntimeError(
                    "触发抖音风控（4031），请稍后重试或改用手机号短信验证码登录"
                )
            await asyncio.sleep(poll_ms / 1000)
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


# ===================== 手机号短信验证码登录（备选方案） =====================


def format_mobile(raw: str) -> str:
    """归一化为 '+86 <号码>'（服务端要求国家码与号码间有一个空格）"""
    for ch in (" ", "-", "(", ")"):
        raw = raw.replace(ch, "")
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("+86"):
        return "+86 " + raw[3:]
    if raw.startswith("+"):
        return raw
    if raw.startswith("86") and len(raw) > 11:
        return "+86 " + raw[2:]
    return "+86 " + raw


class SmsLoginSession(QrLoginSession):
    """手机号 + 短信验证码登录会话（无需扫码）。

    status: idle -> sms_sent -> success / error
    """

    def __init__(self) -> None:
        super().__init__()
        self.mobile = ""
        self.mobile_masked = ""

    async def send_code(self, mobile: str) -> None:
        """发送短信验证码"""
        mobile = format_mobile(mobile)
        if not mobile:
            raise RuntimeError("手机号为空")
        await self.ttwid_check()
        self.mobile = mobile
        resp = await self.call(
            "/passport/web/send_code/",
            None,
            {
                "mix_mode": "1",
                "mobile": xor5(mobile),
                "type": xor5("24"),
                "is6Digits": "1",
                "fixed_mix_mode": "1",
            },
        )
        if (resp or {}).get("message") != "success":
            desc = (resp or {}).get("data", {}).get("description") or (resp or {}).get("message") or "未知错误"
            err_code = int(((resp or {}).get("data") or {}).get("error_code") or 0)
            # 限流（error_code=7 或文案含频繁）：本服务合成指纹被风控时手机号也会一并限流
            if err_code == 7 or re.search(r"太频繁|频繁操作|操作频繁|访问太频繁", desc):
                raise RuntimeError(
                    f"发送验证码失败：{desc}\n"
                    "本服务使用合成指纹，扫码与手机号会同时被限流。\n"
                    "请改用真实浏览器登录 douyin.com → Cookie-Editor 导出 Cookie JSON "
                    "→ 粘到配置页「Cookie JSON」文本框。"
                )
            raise RuntimeError(f"发送验证码失败：{desc}")
        self.mobile_masked = str((resp.get("data") or {}).get("mobile") or mobile)
        self.status = "sms_sent"
        self.message = f"已向 {self.mobile_masked} 发送短信验证码"

    async def submit_code(self, code: str) -> None:
        """验证码登录；成功后 cookies 就绪"""
        if self.status != "sms_sent" or not self.mobile:
            raise RuntimeError("请先发送短信验证码")
        code = code.strip()
        if not re.fullmatch(r"\d{4,8}", code):
            raise RuntimeError("验证码格式不正确")
        resp = await self.call(
            "/passport/web/sms_login/",
            None,
            {
                "service": NEXT_URL,
                "mix_mode": "1",
                "mobile": xor5(self.mobile),
                "code": xor5(code),
                "fixed_mix_mode": "1",
                "login_only": "true",
            },
        )
        if (resp or {}).get("message") != "success":
            desc = (resp or {}).get("data", {}).get("description") or (resp or {}).get("message") or "未知错误"
            err_code = int(((resp or {}).get("data") or {}).get("error_code") or 0)
            if err_code == 7 or re.search(r"太频繁|频繁操作|操作频繁|访问太频繁", desc):
                raise RuntimeError(
                    f"验证码登录失败：{desc}\n"
                    "本服务使用合成指纹，扫码与手机号会同时被限流。\n"
                    "请改用真实浏览器登录 douyin.com → Cookie-Editor 导出 Cookie JSON "
                    "→ 粘到配置页「Cookie JSON」文本框。"
                )
            raise RuntimeError(f"验证码登录失败：{desc}")
        if not self.jar.has("sessionid"):
            raise RuntimeError("登录成功但未拿到 sessionid")
        self.cookies = [
            {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
            for name, value in self.jar.store.items()
        ]
        self.status = "success"
        self.message = "登录成功"
