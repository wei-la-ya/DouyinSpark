"""续火执行器：逐账号拉收件箱总览 -> 补会话 -> 过滤已续 -> 发消息 -> 昵称/抖音号/头像变更检测"""
import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict
from zoneinfo import ZoneInfo

from gsuid_core.logger import logger

from .api import (
    DouyinApiError,
    build_cookie_header,
    create_conversation,
    fetch_user_profile,
    get_cookie_value,
    send_text_message,
)
from .conversations import fetch_inbox_overview
from .database import DyAccount, DyTarget
from .yiyan import pick_yiyan
from ..douyin_config import dy_config

TZ = ZoneInfo("Asia/Shanghai")
PLACEHOLDERS = {"account", "friend", "yiyan", "from", "date", "time", "weekday"}


class AccountResult(TypedDict):
    user_id: str
    bot_id: str
    account_name: str
    sent: int
    renames: List[str]
    skipped: List[str]


class SparkResult(TypedDict):
    sent: int
    successes: List[AccountResult]
    failures: List[Dict[str, str]]


def normalize_template(template: str) -> str:
    if not template:
        return ""
    unknown = {name for name in _placeholder_names(template) if name not in PLACEHOLDERS}
    if unknown:
        raise ValueError(f"消息模板存在未识别占位符：{'、'.join(sorted(unknown))}")
    return template.replace("\\n", "\n")


def _placeholder_names(template: str) -> List[str]:
    import re
    return [m.group(1) for m in re.finditer(r"\{\{\s*([a-zA-Z]+)\s*\}\}", template)]


def render_template(template: str, account: str, friend: str, yiyan: Optional[dict]) -> str:
    now = datetime.now(TZ)
    values: Dict[str, str] = {
        "account": account,
        "friend": friend,
        "yiyan": (yiyan or {}).get("hitokoto", ""),
        "from": (yiyan or {}).get("from", ""),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()],
    }
    import re
    return re.sub(r"\{\{\s*([a-zA-Z]+)\s*\}\}", lambda m: values.get(m.group(1), ""), template)


def _is_today(ms: int) -> bool:
    return datetime.fromtimestamp(ms / 1000, TZ).date() == datetime.now(TZ).date()


async def run_spark(user_id: Optional[str] = None, account_name: Optional[str] = None) -> SparkResult:
    """执行续火。user_id 为空 = 全部用户；account_name 指定单个账号。"""
    stored = await DyAccount.list_accounts(user_id)
    if account_name:
        stored = [a for a in stored if a.name == account_name]
        if not stored:
            raise ValueError(f"未找到名为“{account_name}”的账号")
    if not stored:
        raise ValueError("尚未添加账号，请私聊机器人发送 dy添加账号")

    sent = 0
    successes: List[AccountResult] = []
    failures: List[Dict[str, str]] = []
    for account in stored:
        try:
            result = await _run_account(account)
            sent += result["sent"]
            successes.append(result)
        except Exception as e:
            failures.append({"user_id": account.user_id, "bot_id": account.bot_id, "account_name": account.name, "message": str(e)})
    return {"sent": sent, "successes": successes, "failures": failures}


async def _run_account(account: DyAccount) -> AccountResult:
    cookies = account.cookies
    import json
    cookie_list = json.loads(cookies)
    cookie_header = build_cookie_header(cookie_list)
    if not get_cookie_value(cookie_list, "sessionid"):
        raise ValueError("Cookie 中缺少 sessionid，请修改账号更新 Cookie")

    # imapi HTTP 通道要求设备指纹注册（access_key 基于 device_id 计算）。
    # 没有这一步服务端会返回 StatusCode_130 静默拒绝所有 HTTP imapi 请求。
    # 连接 frontier WS（jumpbyte-bot 用的同一端点）一次即可注册，缓存 1 小时。
    device_id = account.self_uid or "0"
    if device_id != "0":
        try:
            from .ws_init import ensure_device_registered
            ok = await ensure_device_registered(cookies, device_id, timeout=10.0)
            if not ok:
                # 注册失败不阻塞——下面会再失败一次给出更具体错误
                logger.info(f"[{account.name}] WS 设备注册失败，继续尝试 imapi")
        except Exception as e:
            logger.info(f"[{account.name}] WS 设备注册异常：{e}")

    # 收件箱总览：selfUid + 会话补全 + 「今天已续过」过滤依据
    try:
        overview = await fetch_inbox_overview(cookie_header)
    except DouyinApiError as e:
        if e.kind == "parse":
            # 响应非 protobuf：基本就是 Cookie 失效或被风控，给用户可执行的下一步
            raise ValueError(
                f"拉取会话列表失败：{e}。可能是 Cookie 失效或被风控，请发送 dy修改账号 更新 Cookie 后重试"
            ) from e
        raise
    self_uid = str(overview["self_uid"] or "") or account.self_uid
    if self_uid and self_uid != account.self_uid:
        await DyAccount.update_self_uid(account.id, self_uid)
    by_sec_uid: Dict[str, Any] = overview["by_sec_uid"]

    config_template = dy_config.get_config("MessageTemplate").data
    template = normalize_template(account.message_template or config_template or "")
    webid = get_cookie_value(cookie_list, "s_v_web_id")
    uifid = get_cookie_value(cookie_list, "UIFID")
    template_b64 = dy_config.get_config("ImTemplateB64").data or None
    skip_if_sent_today = dy_config.get_config("SkipIfSentToday").data
    min_interval = dy_config.get_config("MinIntervalSec").data
    max_interval = dy_config.get_config("MaxIntervalSec").data

    targets = await DyTarget.list_targets(account.id)
    if not targets:
        raise ValueError("该账号尚未添加续火目标，请发送 dy添加好友 在网页中拉取会话列表选择")

    needs_yiyan = not template or ("{{yiyan}}" in template or "{{from}}" in template)
    target_errors: List[str] = []
    renames: List[str] = []
    skipped: List[str] = []
    sent = 0
    first = True

    for target in targets:
        inbox_entry = by_sec_uid.get(target.sec_uid)
        # 0. 今天已经发过的直接跳过（火花每天只需发一次）
        if skip_if_sent_today and inbox_entry and inbox_entry["last_self_message_ms"] and _is_today(inbox_entry["last_self_message_ms"]):
            skipped.append(target.nickname or target.unique_id or target.sec_uid[:12])
            logger.info(f"[{account.name}] 今天已续过，跳过：{target.nickname or target.sec_uid[:12]}")
            continue

        if first:
            first = False
        else:
            await asyncio.sleep(random.uniform(min_interval, max_interval))

        try:
            # 1. 刷新昵称/抖音号/头像（前台 ID->昵称 映射，变更自动更新）
            profile = await fetch_user_profile(cookie_header, target.sec_uid, webid=webid, uifid=uifid)
            if (profile["nickname"] and profile["nickname"] != target.nickname) \
                    or (profile["unique_id"] and profile["unique_id"] != target.unique_id) \
                    or (profile["avatar"] and profile["avatar"] != target.avatar):
                old_name = target.nickname or "（未知）"
                changed = await DyTarget.update_target_profile(
                    target.id,
                    profile["nickname"] or target.nickname,
                    profile["unique_id"] or target.unique_id,
                    profile["avatar"] or target.avatar,
                )
                if changed and profile["nickname"] and profile["nickname"] != old_name:
                    renames.append(f"{old_name} 已改名为 {profile['nickname']}")
                    target.nickname = profile["nickname"]
                    logger.info(f"[{account.name}] 目标昵称变更：{old_name} -> {profile['nickname']}")

            # 2. 补全会话信息：优先收件箱已有会话（同时刷新过期的 short_id/ticket），陌生人兜底 create
            if not target.conversation_id:
                if inbox_entry:
                    await DyTarget.update_target_conversation(
                        target.id, str(profile["uid"]), inbox_entry["conversation_id"], inbox_entry["conversation_short_id"], inbox_entry.get("ticket", "")
                    )
                    target.conversation_id = inbox_entry["conversation_id"]
                    target.conversation_short_id = inbox_entry["conversation_short_id"]
                    logger.info(f"[{account.name}] 已从收件箱补全会话：{target.nickname or target.sec_uid[:12]}")
                else:
                    created = await create_conversation(
                        cookie_header,
                        receiver_uid=profile["uid"],
                        sender_uid=self_uid or None,
                        template_b64=template_b64,
                        cookies=cookie_list,
                        device_id=device_id,
                    )
                    await DyTarget.update_target_conversation(
                        target.id, str(profile["uid"]), str(created["conversation_id"]), str(created["conversation_short_id"]), str(created.get("ticket", ""))
                    )
                    target.conversation_id = created["conversation_id"]
                    target.conversation_short_id = created["conversation_short_id"]
                    logger.info(f"[{account.name}] 已创建会话：{target.nickname}")
            elif inbox_entry and (
                inbox_entry["conversation_short_id"] != target.conversation_short_id
                or inbox_entry.get("ticket", "") != (target.ticket or "")
            ):
                # DB 有老 conv_id 但 short_id/ticket 已过期（服务端会轮换），用最新的覆盖
                # 实测：send 用过期的 short_id 服务端返 status_code=7905「你已不在群聊内」
                await DyTarget.update_target_conversation(
                    target.id, str(profile["uid"]),
                    inbox_entry["conversation_id"], inbox_entry["conversation_short_id"],
                    inbox_entry.get("ticket", ""),
                )
                target.conversation_id = inbox_entry["conversation_id"]
                target.conversation_short_id = inbox_entry["conversation_short_id"]
                target.ticket = inbox_entry.get("ticket", "")
                logger.info(
                    f"[{account.name}] 刷新会话 short_id（防 7905 风控）：{target.nickname or target.sec_uid[:12]}"
                )

            # 3. 发送消息（后台全程按 ID 寻址，与昵称无关）
            # 关键：每次 send 都用 inbox_entry 的最新 short_id（来自本次 fetch_inbox_overview），
            # 不依赖 DB 缓存。DB 只在 inbox_entry 缺失时作兜底。
            yiyan = await pick_yiyan() if needs_yiyan else None
            if template:
                message = render_template(template, account.name, target.nickname or target.sec_uid, yiyan)
            elif yiyan is not None:
                include_source = dy_config.get_config("IncludeSource").data
                message = f'{yiyan["hitokoto"]}\n——「{yiyan["from"]}」' if include_source else yiyan["hitokoto"]
            else:
                message = "续火🔥"
            # 用 inbox_entry 的最新 short_id（如果有），否则用 DB 缓存（兜底陌生人场景）
            send_conv_id = inbox_entry["conversation_id"] if inbox_entry else target.conversation_id
            send_short_id = (
                str(inbox_entry["conversation_short_id"]) if inbox_entry
                else target.conversation_short_id
            )
            send_ticket = inbox_entry["ticket"] if inbox_entry else (target.ticket or "")
            # 发送消息（带 1 次短 ID 重试：服务端短期 ID 会过期，失败时用最新值再试）
            for _attempt in range(2):
                try:
                    await send_text_message(
                        cookie_header,
                        conversation_id=send_conv_id,
                        conversation_short_id=send_short_id,
                        text=message,
                        template_b64=template_b64,
                        cookies=cookie_list,
                        device_id=account.self_uid or "0",
                        self_uid=account.self_uid or "",
                    )
                    sent += 1
                    logger.info(f"[{account.name}] 已发送消息：{target.nickname}（ID: {target.sec_uid[:12]}…）")
                    # 异步把最新 short_id 写回 DB（fire and forget，不阻塞 send）
                    if inbox_entry:
                        try:
                            await DyTarget.update_target_conversation(
                                target.id, str(profile["uid"]),
                                inbox_entry["conversation_id"],
                                str(inbox_entry["conversation_short_id"]),
                                inbox_entry["ticket"],
                            )
                        except Exception:
                            pass
                    break
                except DouyinApiError as e:
                    if _attempt == 0 and inbox_entry and "7905" in str(e):
                        # 短 ID 在 fetch_inbox_overview 之后又被服务端轮换，重新拉一次
                        try:
                            overview2 = await fetch_inbox_overview(cookie_header)
                            fresh = overview2.get("by_sec_uid", {}).get(target.sec_uid)
                            if fresh and str(fresh["conversation_short_id"]) != send_short_id:
                                send_conv_id = fresh["conversation_id"]
                                send_short_id = str(fresh["conversation_short_id"])
                                send_ticket = fresh["ticket"]
                                logger.info(
                                    f"[{account.name}] 短 ID 被服务端轮换，重新拉取后重试：{target.nickname or target.sec_uid[:12]}"
                                )
                                continue
                        except Exception:
                            pass
                    raise
        except DouyinApiError as e:
            target_errors.append(f"{target.nickname or target.sec_uid[:12]}：{e}")
            logger.warning(f"[{account.name}] 目标 {target.sec_uid[:12]}… 发送失败: {e}")
        except Exception as e:
            target_errors.append(f"{target.nickname or target.sec_uid[:12]}：{e}")
            logger.warning(f"[{account.name}] 目标 {target.sec_uid[:12]}… 发送失败: {e}")

    if sent == 0 and target_errors:
        raise ValueError("\n".join(target_errors))
    if target_errors:
        logger.warning(f"[{account.name}] 部分目标发送失败：\n" + "\n".join(target_errors))
    return {
        "user_id": account.user_id,
        "bot_id": account.bot_id,
        "account_name": account.name,
        "sent": sent,
        "renames": renames,
        "skipped": skipped,
    }
