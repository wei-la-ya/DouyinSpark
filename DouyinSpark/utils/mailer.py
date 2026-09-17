"""SMTP 邮件通知"""
from email.header import Header
from email.mime.text import MIMEText

import aiosmtplib

from gsuid_core.logger import logger

from ..douyin_config import dy_config


def _smtp_ready() -> bool:
    if not dy_config.get_config("SmtpEnabled").data:
        return False
    host = dy_config.get_config("SmtpHost").data
    username = dy_config.get_config("SmtpUsername").data
    password = dy_config.get_config("SmtpPassword").data
    return bool(host and username and password)


async def send_mail(to: str, subject: str, text: str) -> bool:
    """发送纯文本邮件；SMTP 未配置完整时返回 False"""
    if not _smtp_ready():
        logger.warning("[DouyinSpark] SMTP 已开启但配置不完整，跳过邮件")
        return False
    message = MIMEText(text, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    sender = dy_config.get_config("SmtpFrom").data or dy_config.get_config("SmtpUsername").data
    message["From"] = sender
    message["To"] = to
    try:
        await aiosmtplib.send(
            message,
            hostname=dy_config.get_config("SmtpHost").data,
            port=dy_config.get_config("SmtpPort").data,
            username=dy_config.get_config("SmtpUsername").data,
            password=dy_config.get_config("SmtpPassword").data,
            use_tls=dy_config.get_config("SmtpSecure").data,
        )
        logger.info(f"[DouyinSpark] 邮件已发送至 {to}")
        return True
    except Exception as e:
        logger.error(f"[DouyinSpark] 发送邮件失败: {e}")
        return False
