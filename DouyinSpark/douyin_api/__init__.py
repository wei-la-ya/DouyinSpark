"""DouyinSpark 账号配置网页：一次性链接 + 纯API扫码登录 + 会话列表点选续火目标"""
import asyncio
import json
import re
import secrets
import time
from typing import Any, Dict, Optional

import httpx

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app

from ..utils.database import DyAccount, DyTarget, DyUserPref
from ..douyin_config import dy_config
from ..utils.runner import normalize_template
from .page import _SETUP_PAGE_HTML

PAGE_PREFIX = "/douyin-spark"
LINK_EXPIRES_MINUTES = 10

setup_sessions: Dict[str, Dict[str, Any]] = {}
scan_sessions: Dict[str, Any] = {}


_PUBLIC_IP_SOURCES = [
    "https://api.ipify.org/?format=json",
    "https://httpbin.org/ip",
    "https://icanhazip.com",
]

# 公网 IP 探测结果缓存（进程内，避免每次发链接都请求外网）
_public_ip_cache: Optional[str] = None


async def _get_public_ip() -> str:
    """探测本机公网 IP（多源容错，参考 NTEUID 的做法）"""
    global _public_ip_cache
    if _public_ip_cache:
        return _public_ip_cache
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        for url in _PUBLIC_IP_SOURCES:
            try:
                resp = await client.get(url)
                text = resp.text.strip()
                if url.endswith("format=json"):
                    text = resp.json()["ip"]
                elif "httpbin" in url:
                    text = resp.json()["origin"]
                if re.fullmatch(r"[\d.]+", text.split(",")[0].strip()):
                    _public_ip_cache = text.split(",")[0].strip()
                    return _public_ip_cache
            except Exception:
                continue
    raise RuntimeError("无法探测公网 IP，请在配置中填写「网页配置服务对外地址」")


def _is_lan_host(host: str) -> bool:
    return host in ("0.0.0.0", "::", "localhost", "127.0.0.1") or host.startswith(("192.168.", "10.", "172."))


async def create_setup_link(user_id: str, bot_id: str, account_id: Optional[int] = None) -> tuple[str, int]:
    """创建一次性配置链接，返回 (url, 有效分钟数)。

    地址三级回落（参考 NTEUID 设计）：
    配置「配置服务地址」 > Core 的 HOST/PORT > HOST 为局域网时自动探测公网 IP。
    """
    token = secrets.token_hex(32)
    setup_sessions[token] = {
        "user_id": user_id,
        "bot_id": bot_id,
        "account_id": account_id,
        "expires_at": time.time() + LINK_EXPIRES_MINUTES * 60,
    }
    base = dy_config.get_config("SetupServiceUrl").data.strip().rstrip("/")
    if base:
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
    else:
        host = core_config.get_config("HOST")
        port = core_config.get_config("PORT")
        if _is_lan_host(host):
            host = await _get_public_ip()
        base = f"http://{host}:{port}"
    return f"{base}{PAGE_PREFIX}/setup/{token}", LINK_EXPIRES_MINUTES


def _get_session(token: str) -> Optional[Dict[str, Any]]:
    session = setup_sessions.get(token)
    if not session:
        return None
    if session["expires_at"] < time.time():
        setup_sessions.pop(token, None)
        return None
    return session


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status)


def _ok(**kwargs: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **kwargs})


# ===================== 页面 =====================

@app.get(PAGE_PREFIX + "/setup/{token}", response_class=HTMLResponse, include_in_schema=False)
async def setup_page(token: str) -> HTMLResponse:
    session = _get_session(token)
    if not session:
        return HTMLResponse(_simple_page("链接无效或已过期，请重新发送命令获取新链接。"))
    editing = session["account_id"] is not None
    if editing:
        accounts = await DyAccount.list_accounts(session["user_id"], session["bot_id"])
        account = next((a for a in accounts if a.id == session["account_id"]), None)
        if account is None:
            return HTMLResponse(_simple_page("账号不存在。"))
        targets = await DyTarget.list_targets(account.id)
        initial = {
            "name": account.name,
            "messageTemplate": account.message_template,
            "targets": [
                {"secUid": t.sec_uid, "nickname": t.nickname, "uniqueId": t.unique_id, "avatar": t.avatar}
                for t in targets
            ],
        }
    else:
        initial = {"name": "", "messageTemplate": "", "targets": []}
    pref = await DyUserPref.get_pref(session["user_id"], session["bot_id"])
    initial["email"] = pref.email
    initial["successEmailEnabled"] = pref.success_email_enabled
    return HTMLResponse(_render_setup_page(token, initial, editing))


def _simple_page(message: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>抖音续火</title>'
        f"<body><p>{message}</p></body></html>"
    )


def _render_setup_page(token: str, initial: Dict[str, Any], editing: bool) -> str:
    data = json.dumps(initial, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = _SETUP_PAGE_HTML
    page = page.replace("__TITLE__", "修改抖音账号" if editing else "添加抖音账号")
    page = page.replace("__TOKEN__", token)
    page = page.replace("__DATA__", data)
    page = page.replace("__PREFIX__", PAGE_PREFIX)
    page = page.replace("__SUBMIT_LABEL__", "保存修改" if editing else "添加账号")
    return page


# ===================== 扫码登录（纯 API） =====================


async def _persist_login_to_db(token: str, session_obj: Any) -> None:
    """登录成功后立即入库 Cookie + 账号名。

    幂等：同一会话只入库一次；第二次调用跳过。新增场景（QR / SMS）调用此函数即可，
    返回时 name / uid 已在数据库，account_id 写入 session，前端不再需要手动提交 Cookie。
    """
    setup_session = _get_session(token)
    if setup_session is None or session_obj.cookies is None:
        return
    # 已入库过（编辑场景或重复轮询），跳过
    if setup_session.get("account_id") is not None:
        return
    api_name = (session_obj.screen_name or "").strip() or "未命名抖音账号"
    account = await DyAccount.add_account(
        setup_session["user_id"],
        setup_session["bot_id"],
        api_name[:40],
        json.dumps(session_obj.cookies, ensure_ascii=False),
        "",
    )
    setup_session["account_id"] = account.id
    logger.info(
        f"[DouyinSpark] 登录成功已入库 token={token[:8]}... account_id={account.id} name={api_name!r}"
    )


@app.post(PAGE_PREFIX + "/api/scan/start/{token}", include_in_schema=False)
async def scan_start(token: str) -> JSONResponse:
    if not _get_session(token):
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    return await _start_scan(token, force=False)


@app.post(PAGE_PREFIX + "/api/scan/refresh/{token}", include_in_schema=False)
async def scan_refresh(token: str) -> JSONResponse:
    if not _get_session(token):
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    return await _start_scan(token, force=True)


async def _start_scan(token: str, force: bool) -> JSONResponse:
    from ..utils.qrlogin import QrLoginSession

    current = scan_sessions.get(token)
    if not force and current is not None and current.status in ("waiting", "scanned", "sms") and current.qr:
        return _ok(status=current.status, qr=current.qr, message=current.message)
    old = scan_sessions.pop(token, None)
    if old is not None:
        old.cancel()
        await old.aclose()
    scan = QrLoginSession()
    scan_sessions[token] = scan
    try:
        await scan.start()
        # 等二维码就绪（start 内部异步获取）
        for _ in range(40):
            if scan.qr or scan.status == "error":
                break
            await asyncio.sleep(0.25)
        if not scan.qr:
            raise RuntimeError(scan.error or "获取二维码失败")
        logger.info("[DouyinSpark] 扫码登录二维码已通过 API 获取")
        return _ok(status="waiting", qr=scan.qr)
    except Exception as e:
        scan_sessions.pop(token, None)
        logger.error(f"[DouyinSpark] 启动扫码登录失败: {e}")
        return _fail(f"启动扫码登录失败：{e}")


@app.get(PAGE_PREFIX + "/api/scan/status/{token}", include_in_schema=False)
async def scan_status(token: str) -> JSONResponse:
    if not _get_session(token):
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    scan = scan_sessions.get(token)
    if scan is None:
        return _ok(status="idle")
    if scan.status == "error":
        message = scan.error or "扫码登录失败。"
        scan_sessions.pop(token, None)
        return _ok(status="error", message=message)
    if scan.status == "success":
        await _persist_login_to_db(token, scan)
        setup_session = _get_session(token) or {}
        return _ok(
            status="success",
            name=scan.screen_name or "",
            accountId=setup_session.get("account_id"),
            message="登录成功，Cookie 已自动入库。请填写下方消息模板/邮箱/好友后保存。",
        )
    if scan.status == "sms":
        return _ok(status="sms", message=scan.message or "请输入短信验证码。")
    if scan.status == "scanned":
        return _ok(status="waiting", message=scan.message or "已扫码，请在抖音 App 上确认。")
    return _ok(status="waiting", message=scan.message or "")


@app.post(PAGE_PREFIX + "/api/scan/sms/{token}", include_in_schema=False)
async def scan_sms(token: str, request: Request) -> JSONResponse:
    if not _get_session(token):
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    scan = scan_sessions.get(token)
    if scan is None or scan.status != "sms":
        return _fail("当前不在短信验证环节。")
    body = await request.json()
    code = str(body.get("code", "")).strip()
    if not re.fullmatch(r"\d{4,8}", code):
        return _fail("请输入 4 到 8 位短信验证码。")
    scan.submit_sms_code(code)
    logger.info("[DouyinSpark] 已提交短信验证码")
    return _ok(message="验证码已提交，请等待登录结果。")


# ===================== 手机号短信验证码登录（扫码备选） =====================

sms_sessions: Dict[str, Any] = {}


@app.post(PAGE_PREFIX + "/api/sms/send/{token}", include_in_schema=False)
async def sms_send(token: str, request: Request) -> JSONResponse:
    if not _get_session(token):
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    from ..utils.qrlogin import SmsLoginSession

    body = await request.json()
    mobile = str(body.get("mobile", "")).strip()
    if not mobile:
        return _fail("请输入手机号")
    old = sms_sessions.pop(token, None)
    if old is not None:
        await old.aclose()
    session = SmsLoginSession()
    sms_sessions[token] = session
    try:
        await session.send_code(mobile)
        return _ok(status=session.status, message=session.message)
    except Exception as e:
        sms_sessions.pop(token, None)
        logger.warning(f"[DouyinSpark] 发送短信验证码失败: {e}")
        return _fail(str(e))


@app.post(PAGE_PREFIX + "/api/sms/submit/{token}", include_in_schema=False)
async def sms_submit(token: str, request: Request) -> JSONResponse:
    if not _get_session(token):
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    session = sms_sessions.get(token)
    if session is None:
        return _fail("请先发送短信验证码")
    body = await request.json()
    code = str(body.get("code", "")).strip()
    try:
        await session.submit_code(code)
        await _persist_login_to_db(token, session)
        setup_session = _get_session(token) or {}
        logger.info(f"[DouyinSpark] 短信登录成功并入库")
        return _ok(
            status="success",
            name=session.screen_name or "",
            accountId=setup_session.get("account_id"),
            message="短信登录成功，Cookie 已自动入库。请填写下方消息模板/邮箱/好友后保存。",
        )
    except Exception as e:
        return _fail(str(e))


# ===================== 会话列表 =====================

@app.post(PAGE_PREFIX + "/api/conversations/{token}", include_in_schema=False)
async def conversations(token: str) -> JSONResponse:
    session = _get_session(token)
    if not session:
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    if session.get("account_id") is None:
        return _fail("请先完成扫码或短信登录，再拉取会话列表")
    from ..utils.conversations import list_conversations

    accounts = await DyAccount.list_accounts(session["user_id"], session["bot_id"])
    account = next((a for a in accounts if a.id == session["account_id"]), None)
    if account is None:
        return _fail("账号不存在")
    try:
        cookies = json.loads(account.cookies)
    except json.JSONDecodeError:
        return _fail("账号 Cookie 数据损坏，请重新登录")
    try:
        people = await list_conversations(
            cookies,
            on_progress=lambda m: logger.info(f"[DouyinSpark] 会话拉取：{m}"),
            profile_fetch_limit=int(dy_config.get_config("ProfileFetchLimit").data),
        )
        return _ok(list=[
            {
                "secUid": p["sec_uid"],
                "uid": p["uid"],
                "nickname": p.get("nickname", ""),
                "uniqueId": p.get("unique_id", ""),
                "avatar": p.get("avatar", ""),
                "conversationId": p.get("conversation_id", ""),
                "conversationShortId": p.get("conversation_short_id", ""),
                "ticket": p.get("ticket", ""),
            }
            for p in people
        ])
    except Exception as e:
        logger.error(f"[DouyinSpark] 拉取会话列表失败: {e}")
        return _fail(f"拉取会话列表失败：{e}")


# ===================== 保存 =====================

_SEC_UID_RE = re.compile(r"^MS4w[\w-]{10,}$")


@app.post(PAGE_PREFIX + "/api/setup/{token}", include_in_schema=False)
async def save_setup(token: str, request: Request) -> JSONResponse:
    """保存配置（账号名 / 消息模板 / 续火目标 / 邮箱）。

    新流程：Cookie 已在 QR/SMS 登录成功时自动入库，account_id 已写到 session。
    本端点不再接受 cookieText，只更新非 cookie 字段。account_id 必须已存在
    （新账号来自 QR/SMS 成功，编辑账号来自初始会话）。
    """
    session = _get_session(token)
    if not session:
        return _fail("链接无效或已过期，请重新发送命令。", 404)
    if session.get("account_id") is None:
        return _fail("请先完成扫码或短信登录，再填写配置")
    body = await request.json()
    editing = True  # account_id 已存在（无论来自 QR 入库还是编辑会话）

    name = str(body.get("name", "")).strip()
    if not name or len(name) > 40:
        return _fail("账号名称必填且不能超过 40 个字符")
    accounts = await DyAccount.list_accounts(session["user_id"], session["bot_id"])
    if any(a.name == name and a.id != session["account_id"] for a in accounts):
        return _fail(f"已存在名为“{name}”的账号")

    message_template = str(body.get("messageTemplate", "")).strip()
    try:
        normalize_template(message_template)
    except ValueError as e:
        return _fail(str(e))

    raw_targets = body.get("targets")
    targets = None
    if isinstance(raw_targets, list):
        targets = [
            {
                "sec_uid": str(t["secUid"]),
                "uid": str(t.get("uid", "")),
                "nickname": str(t.get("nickname", ""))[:60],
                "unique_id": str(t.get("uniqueId", ""))[:60],
                "avatar": str(t.get("avatar", ""))[:500],
                "conversation_id": str(t.get("conversationId", "")),
                "conversation_short_id": str(t.get("conversationShortId", "")),
                "ticket": str(t.get("ticket", "")),
            }
            for t in raw_targets
            if isinstance(t, dict) and isinstance(t.get("secUid"), str) and _SEC_UID_RE.match(str(t.get("secUid")))
        ]

    try:
        account = next((a for a in accounts if a.id == session["account_id"]), None)
        if account is None:
            return _fail("账号不存在，请重新发送修改命令")
        # 只更新 name + message_template，cookies 保持登录时入库的版本不动
        await DyAccount.update_account(
            session["account_id"], session["user_id"], name,
            account.cookies,  # 保留原 cookie
            message_template,
        )
        account_id = session["account_id"]
        target_note = ""
        if targets is not None:
            count = await DyTarget.replace_targets(account_id, targets)
            target_note = f"，已保存 {count} 个续火目标"
    except Exception as e:
        logger.error(f"[DouyinSpark] 保存账号失败: {e}")
        return _fail(f"保存失败：{e}", 500)

    email = str(body.get("email", "")).strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _fail("邮箱格式不正确")
    await DyUserPref.save_pref(session["user_id"], session["bot_id"], email, bool(body.get("successEmailEnabled")))

    setup_sessions.pop(token, None)
    old_scan = scan_sessions.pop(token, None)
    if old_scan is not None:
        old_scan.cancel()
        await old_scan.aclose()
    logger.info(f"[DouyinSpark] 用户 {session['user_id']} 配置账号「{name}」{target_note}")
    return _ok(message=f"账号已配置{target_note}。现在可以关闭此页面。")
