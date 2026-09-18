"""DouyinSpark 数据模型：账号表 + 续火目标表"""
from typing import Optional, Dict, Any

from sqlmodel import Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseIDModel, BaseModel, with_session


class DyAccount(BaseModel, table=True):
    """抖音账号表（每用户可多账号）"""
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    name: str = Field(title="账号别名")
    cookies: str = Field(title="Cookie(JSON)")
    message_template: str = Field(default="", title="消息模板")
    self_uid: str = Field(default="", title="自身UID")

    @classmethod
    @with_session
    async def list_accounts(
        cls, session: AsyncSession, user_id: Optional[str] = None, bot_id: Optional[str] = None
    ) -> list["DyAccount"]:
        """列出账号；user_id 为空时列全部"""
        stmt = select(cls)
        if user_id is not None:
            stmt = stmt.where(cls.user_id == user_id)
        if bot_id is not None:
            stmt = stmt.where(cls.bot_id == bot_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def add_account(
        cls, session: AsyncSession, user_id: str, bot_id: str, name: str, cookies: str, message_template: str,
        self_uid: str = "",
    ) -> "DyAccount":
        """新增账号；同 (user_id, bot_id, self_uid) 已存在则就地更新（重复扫码场景）"""
        if self_uid:
            stmt = select(cls).where(
                cls.user_id == user_id, cls.bot_id == bot_id, cls.self_uid == self_uid
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                existing.name = name
                existing.cookies = cookies
                existing.message_template = message_template
                session.add(existing)
                await session.flush()
                await session.refresh(existing)
                return existing
        account = cls(
            user_id=user_id, bot_id=bot_id, name=name, cookies=cookies,
            message_template=message_template, self_uid=self_uid,
        )
        session.add(account)
        await session.flush()
        await session.refresh(account)
        return account

    @classmethod
    @with_session
    async def delete_account(cls, session: AsyncSession, user_id: str, name: str) -> bool:
        """删除账号及其全部目标"""
        stmt = select(cls).where(cls.user_id == user_id, cls.name == name)
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()
        if account is None:
            return False
        await session.execute(delete(DyTarget).where(DyTarget.account_id == account.id))
        await session.delete(account)
        return True

    @classmethod
    @with_session
    async def update_account(
        cls, session: AsyncSession, account_id: int, user_id: str, name: str, cookies: str, message_template: str
    ) -> bool:
        """更新账号（限定归属用户）"""
        stmt = select(cls).where(cls.id == account_id, cls.user_id == user_id)
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()
        if account is None:
            return False
        account.name = name
        account.cookies = cookies
        account.message_template = message_template
        session.add(account)
        return True

    @classmethod
    @with_session
    async def update_self_uid(cls, session: AsyncSession, account_id: int, self_uid: str) -> None:
        """缓存账号自身 uid（从会话列表响应解析）"""
        stmt = select(cls).where(cls.id == account_id)
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()
        if account is not None:
            account.self_uid = self_uid
            session.add(account)


class DyTarget(BaseIDModel, table=True):
    """续火目标表：sec_uid 为寻址主键，昵称/抖音号/头像仅作前台映射展示"""
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    account_id: int = Field(title="账号ID")
    sec_uid: str = Field(title="sec_uid")
    uid: str = Field(default="", title="uid")
    nickname: str = Field(default="", title="昵称")
    unique_id: str = Field(default="", title="抖音号")
    avatar: str = Field(default="", title="头像")
    conversation_id: str = Field(default="", title="会话ID")
    conversation_short_id: str = Field(default="", title="会话短ID")
    ticket: str = Field(default="", title="ticket")

    @classmethod
    @with_session
    async def list_targets(cls, session: AsyncSession, account_id: int) -> list["DyTarget"]:
        """列出账号的全部续火目标"""
        stmt = select(cls).where(cls.account_id == account_id).order_by(cls.id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def replace_targets(cls, session: AsyncSession, account_id: int, targets: list[Dict[str, str]]) -> int:
        """整体替换账号的目标列表（网页配置保存时调用），保留会话信息避免续火时重建会话"""
        await session.execute(delete(cls).where(cls.account_id == account_id))
        for t in targets:
            session.add(cls(
                account_id=account_id,
                sec_uid=t["sec_uid"],
                uid=t.get("uid", ""),
                nickname=t.get("nickname", ""),
                unique_id=t.get("unique_id", ""),
                avatar=t.get("avatar", ""),
                conversation_id=t.get("conversation_id", ""),
                conversation_short_id=t.get("conversation_short_id", ""),
                ticket=t.get("ticket", ""),
            ))
        return len(targets)

    @classmethod
    @with_session
    async def delete_target(cls, session: AsyncSession, account_id: int, target_id: int) -> bool:
        """按 id 删除目标"""
        stmt = select(cls).where(cls.account_id == account_id, cls.id == target_id)
        result = await session.execute(stmt)
        target = result.scalar_one_or_none()
        if target is None:
            return False
        await session.delete(target)
        return True

    @classmethod
    @with_session
    async def update_target_conversation(
        cls, session: AsyncSession, target_id: int, uid: str, conversation_id: str, conversation_short_id: str, ticket: str
    ) -> None:
        """更新目标的会话信息"""
        stmt = select(cls).where(cls.id == target_id)
        result = await session.execute(stmt)
        target = result.scalar_one_or_none()
        if target is not None:
            target.uid = uid
            target.conversation_id = conversation_id
            target.conversation_short_id = conversation_short_id
            target.ticket = ticket
            session.add(target)

    @classmethod
    @with_session
    async def update_target_profile(
        cls, session: AsyncSession, target_id: int, nickname: str, unique_id: str, avatar: str
    ) -> bool:
        """昵称/抖音号/头像变更时更新映射；返回是否有变动"""
        stmt = select(cls).where(cls.id == target_id)
        result = await session.execute(stmt)
        target = result.scalar_one_or_none()
        if target is None:
            return False
        if target.nickname == nickname and target.unique_id == unique_id and target.avatar == avatar:
            return False
        target.nickname = nickname
        target.unique_id = unique_id
        target.avatar = avatar
        session.add(target)
        return True


@site.register_admin
class DyAccountAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="抖音续火账号管理", icon="fa fa-fire")
    model = DyAccount


class DyUserPref(BaseIDModel, table=True):
    """用户偏好：失败通知邮箱 / 成功邮件开关（跨账号）"""
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    user_id: str = Field(title="用户ID", index=True)
    bot_id: str = Field(default="", title="BOT ID", index=True)
    email: str = Field(default="", title="邮箱")
    success_email_enabled: bool = Field(default=False, title="成功邮件")

    @classmethod
    @with_session
    async def get_pref(cls, session: AsyncSession, user_id: str, bot_id: str) -> "DyUserPref":
        """读取用户偏好（不存在时返回未入库的空对象）"""
        stmt = select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        result = await session.execute(stmt)
        pref = result.scalar_one_or_none()
        if pref is None:
            pref = cls(user_id=user_id, bot_id=bot_id)
        return pref

    @classmethod
    @with_session
    async def save_pref(cls, session: AsyncSession, user_id: str, bot_id: str, email: str, success_email_enabled: bool) -> None:
        """写入/更新用户偏好"""
        stmt = select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        result = await session.execute(stmt)
        pref = result.scalar_one_or_none()
        if pref is None:
            pref = cls(user_id=user_id, bot_id=bot_id, email=email, success_email_enabled=success_email_enabled)
        else:
            pref.email = email
            pref.success_email_enabled = success_email_enabled
        session.add(pref)


@site.register_admin
class DyTargetAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="抖音续火目标管理", icon="fa fa-users")
    model = DyTarget


@site.register_admin
class DyUserPrefAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="抖音续火用户偏好", icon="fa fa-envelope")
    model = DyUserPref
