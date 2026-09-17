"""DouyinSpark 命令层：账号/目标管理 + 续火触发 + 帮助"""
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV
from gsuid_core.subscribe import gs_subscribe

from ..utils.database import DyAccount, DyTarget
from ..utils.runner import run_spark
from ..douyin_api import create_setup_link
from ..utils.external import external_setup_flow, external_base_url, send_setup_link

sv = SV("抖音续火", pm=6, area="ALL")

HELP_TEXT = """抖音续火命令一览（前缀 dy）
dy添加账号：发送一次性网页链接，添加账号并在网页中点选续火目标。
dy账号列表：查看自己添加的账号别名和续火目标数量。
dy删除账号 账号名：删除自己的指定账号及其续火目标。
dy修改账号 账号名：私聊获取修改链接，可更新 Cookie 或重选续火目标。
dy添加好友 [账号名]：发链接拉取会话列表勾选新增目标（Cookie 有效无需重扫）。
dy好友列表 [账号名]：查看续火目标的 昵称/抖音号 映射（自动刷新）。
dy删除好友 [账号名] 序号：删除指定续火目标。
dy刷新昵称 [账号名]：批量刷新目标昵称，报告改名情况。
dy续火：执行自己全部账号。
dy续火 账号名：仅执行自己的指定账号。
dy续火 全部（仅主人）：执行所有用户账号。
dy续火 帮助：查看本帮助。"""


def _is_private(ev: Event) -> bool:
    return not ev.group_id


@sv.on_command("续火", block=True)
async def spark(bot: Bot, ev: Event) -> None:
    argument = ev.text.strip()
    if argument == "帮助":
        return await bot.send(HELP_TEXT)
    all_users = argument == "全部"
    if all_users and ev.user_pm != 0:
        return await bot.send("“全部”仅限机器人主人使用。")
    # 未绑定账号时直接引导添加，不走执行流程
    if not all_users and not argument:
        if not await DyAccount.list_accounts(ev.user_id, ev.bot_id):
            return await bot.send("你还没有绑定抖音账号。\n私聊我发送 dy添加账号 即可开始绑定（网页扫码或粘贴 Cookie，勾选好友后自动续火）。")
    await bot.send("正在启动抖音续火，请稍候...")
    try:
        result = await run_spark(None if all_users else ev.user_id, account_name=argument or None)
        lines = [f"抖音续火完成：成功发送 {result['sent']} 条消息。"]
        for item in result["successes"]:
            lines.extend(f"已续过跳过：{name}" for name in item["skipped"])
            lines.extend(f"昵称变更：{name}" for name in item["renames"])
        for item in result["failures"]:
            lines.append(f"失败：{item['account_name']}（{item['message']}）")
        await bot.send("\n".join(lines))
    except ValueError as e:
        # 未绑定账号 / 账号不存在：引导添加而不是报错
        await bot.send(f"{e}\n现在私聊我发送 dy添加账号 即可开始绑定。")
    except Exception as e:
        await bot.send(f"抖音续火失败：{e}")


@sv.on_fullmatch("添加账号")
async def add_account(bot: Bot, ev: Event) -> None:
    if not _is_private(ev):
        return await bot.send("为保护 Cookie，请私聊机器人发送 dy添加账号。")
    url, minutes = await create_setup_link(ev.user_id, ev.bot_id)
    await bot.send(f"请在 {minutes} 分钟内打开链接添加账号：\n{url}")
    await gs_subscribe.add_subscribe("session", "抖音续火结果", ev)


@sv.on_fullmatch("账号列表")
async def account_list(bot: Bot, ev: Event) -> None:
    accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
    if not accounts:
        return await bot.send("你还没有添加账号，请私聊机器人发送 dy添加账号。")
    lines = []
    for account in accounts:
        targets = await DyTarget.list_targets(account.id)
        lines.append(f"- {account.name}（{len(targets)} 个续火目标）")
    await bot.send("你的抖音账号：\n" + "\n".join(lines))


@sv.on_prefix("删除账号")
async def remove_account(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()
    removed = await DyAccount.delete_account(ev.user_id, name)
    await bot.send(f"账号“{name}”已删除，其续火目标一并清除。" if removed else f"未找到名为“{name}”的账号。")


@sv.on_prefix("修改账号")
async def edit_account(bot: Bot, ev: Event) -> None:
    if not _is_private(ev):
        return await bot.send("为保护 Cookie，请私聊机器人发送 dy修改账号 账号名。")
    name = ev.text.strip()
    accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
    account = next((a for a in accounts if a.name == name), None)
    if account is None:
        return await bot.send(f"未找到名为“{name}”的账号。")
    url, minutes = await create_setup_link(ev.user_id, ev.bot_id, account_id=account.id)
    await bot.send(
        f"请在 {minutes} 分钟内打开链接修改账号“{name}”：\n{url}\n可更新 Cookie、消息模板，也可点「拉取会话列表」增删续火目标。"
    )


@sv.on_command("添加好友")
async def add_target_via_web(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()
    accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
    if not accounts:
        return await bot.send("你还没有添加账号，请私聊机器人发送 dy添加账号。")
    account = None
    if name:
        account = next((a for a in accounts if a.name == name), None)
        if account is None:
            return await bot.send(f"未找到名为“{name}”的账号，可用账号：{'、'.join(a.name for a in accounts)}")
    elif len(accounts) == 1:
        account = accounts[0]
    else:
        return await bot.send(f"你有多个账号，请指定账号名：{'、'.join(a.name for a in accounts)}")
    url, minutes = await create_setup_link(ev.user_id, ev.bot_id, account_id=account.id)
    await bot.send(
        f"请在 {minutes} 分钟内打开链接为账号“{account.name}”增删续火目标：\n{url}\n"
        "打开后点击「拉取会话列表」，勾选新人后提交即可；已勾选的目标保持不变，Cookie 未过期无需重新扫码。"
    )


def _short_id(sec_uid: str) -> str:
    return f"{sec_uid[:12]}…{sec_uid[-4:]}" if len(sec_uid) > 16 else sec_uid


def _display(target: DyTarget) -> str:
    name = target.nickname or "（未获取昵称）"
    return f"{name}（抖音号: {target.unique_id}）" if target.unique_id else name


async def _find_account(ev: Event, name: str) -> DyAccount | None:
    accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
    if not accounts:
        return None
    if name:
        return next((a for a in accounts if a.name == name), None)
    return accounts[0] if len(accounts) == 1 else None


@sv.on_command("好友列表")
async def target_list(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()
    accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
    selected = [a for a in accounts if a.name == name] if name else accounts
    if not selected:
        return await bot.send(f"未找到名为“{name}”的账号。" if name else "你还没有添加账号。")
    lines = []
    for account in selected:
        targets = await DyTarget.list_targets(account.id)
        lines.append(f"【{account.name}】共 {len(targets)} 个续火目标")
        if not targets:
            lines.append("  （空，发送 dy添加好友 在网页中拉取会话列表选择）")
            continue
        for index, target in enumerate(targets):
            lines.append(f"  {index + 1}. {_display(target)}")
    lines.append("提示：昵称/抖音号/头像会在每次续火时自动更新。")
    await bot.send("\n".join(lines))


@sv.on_prefix("删除好友")
async def remove_target(bot: Bot, ev: Event) -> None:
    tokens = ev.text.strip().split()
    if not tokens:
        return await bot.send("用法：dy删除好友 [账号名] <序号>\n序号可通过 抖音好友列表 查看。")
    name = ""
    index_text = tokens[0]
    accounts = await DyAccount.list_accounts(ev.user_id, ev.bot_id)
    if len(tokens) >= 2 and any(a.name == tokens[0] for a in accounts):
        name = tokens[0]
        index_text = tokens[1]
    account = await _find_account(ev, name)
    if account is None:
        return await bot.send("账号不存在或有多个账号，请指定账号名。")
    if not index_text.isdigit() or int(index_text) < 1:
        return await bot.send("用法：dy删除好友 [账号名] <序号>")
    targets = await DyTarget.list_targets(account.id)
    index = int(index_text) - 1
    if index >= len(targets):
        return await bot.send(f"序号 {index_text} 不存在，该账号共 {len(targets)} 个续火目标。")
    target = targets[index]
    await DyTarget.delete_target(account.id, target.id)
    await bot.send(f"已删除续火目标：{_display(target)}")


@sv.on_command("刷新昵称")
async def refresh_nicknames(bot: Bot, ev: Event) -> None:
    import json
    from ..utils.api import build_cookie_header, fetch_user_profile, get_cookie_value

    name = ev.text.strip()
    account = await _find_account(ev, name)
    if account is None:
        return await bot.send("账号不存在或有多个账号，请指定账号名。")
    targets = await DyTarget.list_targets(account.id)
    if not targets:
        return await bot.send(f"账号“{account.name}”还没有续火目标。")
    await bot.send(f"正在刷新 {len(targets)} 个目标的昵称…")
    cookies = json.loads(account.cookies)
    cookie_header = build_cookie_header(cookies)
    webid = get_cookie_value(cookies, "s_v_web_id")
    uifid = get_cookie_value(cookies, "UIFID")
    changes = []
    failures = []
    for target in targets:
        try:
            profile = await fetch_user_profile(cookie_header, target.sec_uid, webid=webid, uifid=uifid)
            if profile["nickname"] and profile["nickname"] != target.nickname:
                await DyTarget.update_target_profile(
                    target.id,
                    profile["nickname"],
                    profile["unique_id"] or target.unique_id,
                    profile["avatar"] or target.avatar,
                )
                changes.append(f"{target.nickname or '（未知）'} → {profile['nickname']}")
        except Exception as e:
            failures.append(f"{target.nickname or _short_id(target.sec_uid)}：{e}")
    lines = [f"昵称刷新完成：共 {len(targets)} 个目标，变更 {len(changes)} 个，失败 {len(failures)} 个。"]
    if changes:
        lines.append("变更明细：")
        lines.extend(f"- {c}" for c in changes)
    if failures:
        lines.append("失败明细：")
        lines.extend(f"- {f}" for f in failures)
    await bot.send("\n".join(lines))


# ===================== 定时任务 =====================

from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from gsuid_core.aps import scheduler  # noqa: E402
from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.utils.message import send_msg_to_master  # noqa: E402

from ..douyin_config import dy_config  # noqa: E402


async def _push_result_to_user(user_id: str, bot_id: str, lines: list[str]) -> None:
    """把结果私聊推送给订阅了推送的对应用户"""
    datas = await gs_subscribe.get_subscribe("抖音续火结果")
    for subscribe in datas:
        if subscribe.user_id == user_id and subscribe.bot_id == bot_id:
            try:
                await subscribe.send("\n".join(lines), force_direct=True)
            except Exception as e:
                logger.warning(f"[DouyinSpark] 向用户 {user_id} 推送失败: {e}")


async def daily_spark_job() -> None:
    """每日定时续火：执行全部账号，结果推送给各自用户，汇总推给主人"""
    logger.info("[DouyinSpark] 定时续火任务启动")
    try:
        result = await run_spark()
    except Exception as e:
        logger.error(f"[DouyinSpark] 定时续火失败: {e}")
        await send_msg_to_master(f"抖音续火定时任务失败：{e}")
        return

    # 按用户聚合结果
    by_user: dict[tuple[str, str], dict] = {}
    for item in result["successes"]:
        key = (item["user_id"], item["bot_id"])
        entry = by_user.setdefault(key, {"sent": 0, "skipped": [], "renames": [], "failures": []})
        entry["sent"] += item["sent"]
        entry["skipped"].extend(item["skipped"])
        entry["renames"].extend(item["renames"])
    for item in result["failures"]:
        key = (item["user_id"], item["bot_id"])
        entry = by_user.setdefault(key, {"sent": 0, "skipped": [], "renames": [], "failures": []})
        entry["failures"].append(item)

    for (user_id, bot_id), entry in by_user.items():
        lines = ["抖音自动续火结果"]
        if entry["sent"]:
            lines.append(f"成功发送：{entry['sent']} 条")
        lines.extend(f"已续过跳过：{n}" for n in entry["skipped"])
        lines.extend(f"昵称变更：{n}" for n in entry["renames"])
        lines.extend(f"失败：{f['account_name']}（{f['message']}）" for f in entry["failures"])
        await _push_result_to_user(user_id, bot_id, lines)

    # 主人汇总
    summary = [
        "抖音自动续火汇总",
        f"总发送：{result['sent']} 条",
        f"成功账号：{len(result['successes'])} 个",
        f"失败账号：{len(result['failures'])} 个",
    ]
    if result["failures"]:
        summary.append("失败明细：")
        summary.extend(f"- [{f['user_id']}] {f['account_name']}：{f['message']}" for f in result["failures"])
    await send_msg_to_master("\n".join(summary))
    logger.mark(f"[DouyinSpark] 定时续火完成，发送 {result['sent']} 条")


def _register_spark_job() -> None:
    if not dy_config.get_config("SparkEnabled").data:
        return
    hour, minute = dy_config.get_config("SparkTime").data
    scheduler.add_job(
        daily_spark_job,
        CronTrigger(hour=hour, minute=minute),
        id="douyin_spark_daily",
        replace_existing=True,
    )
    logger.info(f"[DouyinSpark] 已注册每日续火任务：{hour:02d}:{minute:02d}")


_register_spark_job()
