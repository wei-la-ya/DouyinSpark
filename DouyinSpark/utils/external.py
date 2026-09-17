"""外置配置页流程：start(HTTP) -> 发链接 -> listen(WS) -> 写库 -> 通知用户

参考 NTEUID 的外置登录（tyql688/NTEUID + nte-login），但不做共享密钥签名——仅 WS。
外置服务约定见 plugins/DouyinSpark/external/main.py。
"""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Dict, Optional

import httpx

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.subscribe import gs_subscribe

from .mailer import send_mail

from .database import DyAccount, DyTarget, DyUserPref
from ..douyin_config import dy_config

LISTEN_TIMEOUT_S = 600.0
START_TIMEOUT_S = 10.0


def external_base_url() -> str:
    """外置模式开启时返回外置服务地址；未开启或地址为空返回空串"""
    if not dy_config.get_config("UseExternalSetup").data:
        return ""
    raw = dy_config.get_config("SetupServiceUrl").data.strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


async def external_setup_flow(
    bot: Bot,
    ev: Event,
    *,
    account_id: Optional[int] = None,
    action_text: str = "添加账号",
) -> None:
    """外置模式配置流程：start -> 发链接 -> WS 监听 -> 写库 -> 回复结果"""
    base = external_base_url()
    if not base:
        await bot.send("已开启外置配置服务但未填「配置服务地址」，请先在 Web 控制台配置。")
        return
    # 随机会话标识（user_id/bot_id/account_id 已在 start 请求体里，链接不暴露用户 ID）
    auth = secrets.token_hex(16)

    # 编辑模式：把现有账号数据带给外置服务做页面初始值
    initial: Dict[str, Any] = {}
    if account_id is not None:
        accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
        account = next((a for a in accounts if a.id == account_id), None)
        if account is None:
            await bot.send("账号不存在。")
            return
        targets = await DyTarget.list_targets(account.id)
        pref = await DyUserPref.get_pref(ev.user_id, ev.bot_id)
        initial = {
            "name": account.name,
            "messageTemplate": account.message_template,
            "cookieRequired": False,
            "targets": [
                {"secUid": t.sec_uid, "nickname": t.nickname, "uniqueId": t.unique_id, "avatar": t.avatar}
                for t in targets
            ],
            "email": pref.email,
            "successEmailEnabled": pref.success_email_enabled,
        }

    body = {
        "auth": auth,
        "user_id": ev.user_id,
        "bot_id": ev.bot_id,
        "account_id": account_id,
        "initial": initial,
    }
    try:
        async with httpx.AsyncClient(timeout=START_TIMEOUT_S, trust_env=False) as client:
            resp = await client.post(f"{base}/dyspark/start", json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"外置服务 start 返回 HTTP {resp.status_code}：{resp.text[:200]}")
        # 链接用插件配置的地址拼接（不信服务端 base_url，反代子路径下会丢前缀）
        page_url = f"{base}/dyspark/i/{auth}"
    except Exception as e:
        logger.warning(f"[DouyinSpark] 外置 start 失败 user_id={ev.user_id}: {e}")
        await bot.send(f"外置配置服务不可用：{e}")
        return

    await bot.send(f"请在 10 分钟内打开链接{action_text}：\n{page_url}")
    logger.info(f"[DouyinSpark] 外置配置页已发送 user_id={ev.user_id} url={page_url}")

    result = await _listen_ws(base, auth)
    if result is None:
        await bot.send("配置链接已过期，未完成保存。")
        return
    if result["status"] != "success":
        await bot.send(result.get("msg") or "配置失败。")
        return

    payload = result.get("payload") or {}
    try:
        await _save_payload(ev, account_id, payload)
    except Exception as e:
        logger.error(f"[DouyinSpark] 外置结果写库失败: {e}")
        await bot.send(f"保存失败：{e}")
        return
    name = payload.get("name", "")
    count = len(payload.get("targets") or [])
    await bot.send(f"账号「{name}」已{'更新' if account_id is not None else '添加'}，保存 {count} 个续火目标。")


async def _listen_ws(base: str, auth: str) -> Optional[Dict[str, Any]]:
    """WS 监听外置服务会话状态（无密钥签名）"""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        logger.error("[DouyinSpark] 外置 WS 模式需要安装 websockets 库")
        return {"status": "failed", "msg": "缺少 websockets 依赖"}

    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = f"{ws_base}/dyspark/ws/{auth}"
    try:
        async with asyncio.timeout(LISTEN_TIMEOUT_S):
            async with connect(url, open_timeout=START_TIMEOUT_S, proxy=None) as ws:
                async for raw in ws:
                    payload = json.loads(raw)
                    if payload.get("status") in ("success", "failed", "expired"):
                        return payload
    except TimeoutError:
        return None
    except OSError as e:
        logger.warning(f"[DouyinSpark] 外置 WS 连接失败: {e!r}")
        return {"status": "failed", "msg": f"外置服务连接失败：{e}"}
    return None


async def _save_payload(ev: Event, account_id: Optional[int], payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", "")).strip()
    cookies = payload.get("cookies")
    message_template = str(payload.get("message_template", ""))
    targets = payload.get("targets") or []

    if account_id is not None:
        accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
        account = next((a for a in accounts if a.id == account_id), None)
        if account is None:
            raise RuntimeError("账号不存在")
        await DyAccount.update_account(
            account_id, ev.user_id, name,
            json.dumps(cookies, ensure_ascii=False) if cookies else account.cookies,
            message_template,
        )
        aid = account_id
    else:
        account = await DyAccount.add_account(
            ev.user_id, ev.bot_id, name,
            json.dumps(cookies, ensure_ascii=False), message_template,
        )
        aid = account.id

    await DyTarget.replace_targets(aid, [
        {
            "sec_uid": str(t["sec_uid"]),
            "uid": str(t.get("uid", "")),
            "nickname": str(t.get("nickname", ""))[:60],
            "unique_id": str(t.get("unique_id", ""))[:60],
            "avatar": str(t.get("avatar", ""))[:500],
            "conversation_id": str(t.get("conversation_id", "")),
            "conversation_short_id": str(t.get("conversation_short_id", "")),
            "ticket": str(t.get("ticket", "")),
        }
        for t in targets
    ])
    await DyUserPref.save_pref(
        ev.user_id, ev.bot_id,
        str(payload.get("email", "")),
        bool(payload.get("success_email_enabled")),
    )


async def send_setup_link(bot: Bot, ev: Event, url: str, minutes: int, action_text: str) -> bool:
    """隐私链接发送：私聊直接发；群聊主动私聊；私聊失败时发邮箱
    （onebot 直接用 QQ 邮箱 user_id@qq.com，其他平台用 dy绑定邮箱 绑定的邮箱）。

    返回 True 表示链接已送达（任一渠道）。
    """
    text = f"请在 {minutes} 分钟内打开链接{action_text}：" + chr(10) + url
    if not ev.group_id:
        await bot.send(text)
        return True
    # 群聊触发：主动私聊
    try:
        await gs_subscribe.add_subscribe("session", "抖音配置链接", ev)
        datas = await gs_subscribe.get_subscribe("抖音配置链接")
        target = next(
            (sub for sub in datas if sub.user_id == ev.user_id and sub.bot_id == ev.bot_id),
            None,
        )
        if target is None:
            raise RuntimeError("未找到订阅会话")
        await target.send(text, force_direct=True)
        await gs_subscribe.delete_subscribe("session", "抖音配置链接", ev)
        await bot.send("已私聊发送网页链接，请在网页内完成操作。", at_sender=True)
        return True
    except Exception as e:
        # 私聊失败 → 邮箱兜底
        logger.warning(f"[DouyinSpark] 私聊发送配置链接失败: {e}")
        email = ""
        if ev.bot_id == "onebot":
            email = f"{ev.user_id}@qq.com"
        else:
            from .database import DyUserPref
            pref = await DyUserPref.get_pref(ev.user_id, ev.bot_id)
            email = pref.email
        if not email:
            await bot.send(
                "私聊发送失败，且未绑定接收邮箱。\n请私聊我发送 dy绑定邮箱 你的邮箱（如 dy绑定邮箱 12345@qq.com）后重试。",
                at_sender=True,
            )
            return False
        mail_text = text + chr(10) * 2 + "（此邮件由机器人自动发送，请勿回复）"
        if await send_mail(email, "抖音续火账号配置链接", mail_text):
            await bot.send("已将链接发送到你的qq邮箱内，请在网页内完成操作。", at_sender=True)
            return True
        await bot.send("私聊发送失败，邮箱发送也失败了。请检查 SMTP 配置，或私聊我重新发送命令。", at_sender=True)
        return False
