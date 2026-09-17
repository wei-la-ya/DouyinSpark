"""配置默认值"""
from typing import Dict

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsBoolConfig,
    GsIntConfig,
    GsStrConfig,
    GsTimeRConfig,
    GsDivider,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "SparkEnabled": GsBoolConfig(
        title="启用定时续火",
        desc="关闭后不会按计划自动执行，仍可手动发送 抖音续火",
        data=True,
    ),
    "SparkTime": GsTimeRConfig(
        title="每日续火时间",
        desc="每天自动续火的时间点（时:分）",
        data=(0, 10),
    ),
    "MessageTemplate": GsStrConfig(
        title="默认消息模板",
        desc="留空则发送随机一言。占位符：{{account}} 账号别名、{{friend}} 目标昵称、{{yiyan}} 一言正文、{{from}} 一言出处、{{date}} 日期、{{time}} 时间、{{weekday}} 星期；换行写 \\n",
        data="{{friend}}，今天的火花到账啦🔥\\n{{yiyan}}\\n——「{{from}}」\\n{{date}} {{weekday}}",
    ),
    "IncludeSource": GsBoolConfig(
        title="一言附带出处",
        desc="未设置消息模板时，是否在随机一言后附带出处",
        data=True,
    ),
    "MinIntervalSec": GsIntConfig(
        title="发送间隔下限（秒）",
        desc="同一账号相邻两个续火目标之间的随机延时下限，降低风控概率",
        data=3,
        max_value=600,
    ),
    "MaxIntervalSec": GsIntConfig(
        title="发送间隔上限（秒）",
        desc="同一账号相邻两个续火目标之间的随机延时上限",
        data=8,
        max_value=600,
    ),
    "SkipIfSentToday": GsBoolConfig(
        title="跳过今天已续火的会话",
        desc="开启后，续火前检查会话最近消息，今天已经给对方发过的直接跳过（火花每天只需发一次）",
        data=True,
    ),
    "ProfileFetchLimit": GsIntConfig(
        title="昵称补全人数上限",
        desc="拉取会话列表时最多为多少人查询昵称和抖音号；超出的人会显示在列表中但没有昵称",
        data=30,
        max_value=200,
    ),
    "ImTemplateB64": GsStrConfig(
        title="自定义 IM 请求模板",
        desc="一般留空。内置私信请求模板失效时，浏览器 F12 抓取一次真实 message/send 请求体（protobuf 二进制）转 Base64 后粘贴到这里",
        data="",
        secret=True,
    ),
    "UseExternalSetup": GsBoolConfig(
        title="使用外置配置服务",
        desc="开启后 dy添加账号/添加好友/修改账号 走外置服务（HTTP start + WS 回调）；关闭则使用 Core 内嵌配置页",
        data=False,
    ),
    "SetupServiceUrl": GsStrConfig(
        title="配置服务地址",
        desc="外置模式：填外置服务地址（如 https://dyspark-login.example.com，必填）。内置模式：填网页对外地址（如 https://spark.example.com 或内网穿透域名），留空则取 Core 的 HOST/PORT",
        data="",
    ),
    "DividerSmtp": GsDivider(title="SMTP 邮件通知", desc=""),
    "SmtpEnabled": GsBoolConfig(
        title="启用邮件通知",
        desc="开启后可向用户发送续火失败邮件；用户开启成功通知时也会使用此配置",
        data=False,
    ),
    "SmtpHost": GsStrConfig(title="SMTP 主机", desc="邮件服务商的 SMTP 服务器地址，例如 QQ 邮箱为 smtp.qq.com", data="smtp.qq.com"),
    "SmtpPort": GsIntConfig(title="SMTP 端口", desc="常用 SSL 端口为 465", data=465, max_value=65535),
    "SmtpSecure": GsBoolConfig(title="启用 SSL", desc="端口为 465 时通常需要开启", data=True),
    "SmtpUsername": GsStrConfig(title="SMTP 用户名", desc="通常填写完整邮箱地址", data=""),
    "SmtpPassword": GsStrConfig(title="SMTP 授权码", desc="填写邮件服务商生成的 SMTP 授权码，不是邮箱登录密码", data="", secret=True),
    "SmtpFrom": GsStrConfig(title="发件人", desc="邮件中显示的发件人地址；留空时使用 SMTP 用户名", data=""),
}
