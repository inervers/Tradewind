"""SMTP adapter with conservative, stage-aware delivery outcomes."""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal

from app.config import EMAIL_PROVIDER_PRESETS


@dataclass(frozen=True)
class SendOutcome:
    status: Literal["accepted", "failed", "unknown"]
    code: str
    summary: str
    reference: str = ""


class SmtpSender:
    def __init__(self, factory=None, timeout: float = 20.0) -> None:
        self.factory = factory
        self.timeout = timeout

    def _connect(self, config: dict, context: ssl.SSLContext):
        provider = str(config.get("provider", ""))
        if provider not in EMAIL_PROVIDER_PRESETS:
            raise ValueError("不支持的邮箱提供商")
        preset = EMAIL_PROVIDER_PRESETS[provider]
        transport = str(config.get("transport", "ssl"))
        if transport not in {"ssl", "starttls"}:
            raise ValueError("不支持的 SMTP 加密方式")
        port = preset["ssl_port" if transport == "ssl" else "starttls_port"]
        if self.factory is not None:
            return self.factory(preset["host"], port, timeout=self.timeout)
        if transport == "ssl":
            return smtplib.SMTP_SSL(
                preset["host"], port, timeout=self.timeout, context=context
            )
        return smtplib.SMTP(preset["host"], port, timeout=self.timeout)

    @staticmethod
    def _message(job: dict, sender: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = str(job["recipient"])
        message["Subject"] = str(job["subject"])
        message["Message-ID"] = str(job["message_id"])
        message.set_content(str(job["body"]), subtype="plain", charset="utf-8")
        return message

    def send(self, job: dict, config: dict, secret: str) -> SendOutcome:
        address = str(config.get("address", "")).strip()
        if not address or not secret:
            return SendOutcome("failed", "auth", "邮箱账号或授权凭据未配置")
        context = ssl.create_default_context()
        submitting = False
        try:
            with self._connect(config, context) as client:
                if config.get("transport", "ssl") == "starttls":
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                client.login(address, secret)
                submitting = True
                refused = client.send_message(self._message(job, address))
                if refused:
                    return SendOutcome(
                        "failed", "recipient_rejected", "收件地址被邮箱服务器拒绝"
                    )
        except smtplib.SMTPAuthenticationError:
            return SendOutcome("failed", "auth", "邮箱认证失败，请检查授权凭据")
        except smtplib.SMTPRecipientsRefused:
            return SendOutcome(
                "failed", "recipient_rejected", "收件地址被邮箱服务器拒绝"
            )
        except smtplib.SMTPDataError as exc:
            if 500 <= int(exc.smtp_code) < 600:
                return SendOutcome(
                    "failed", "message_rejected", "邮件内容被邮箱服务器明确拒绝"
                )
            return SendOutcome(
                "unknown", "submission_uncertain", "提交结果不确定，请检查发件箱"
            )
        except (TimeoutError, socket.timeout, smtplib.SMTPServerDisconnected):
            if submitting:
                return SendOutcome(
                    "unknown", "submission_uncertain", "提交结果不确定，请检查发件箱"
                )
            return SendOutcome("failed", "network", "无法连接邮箱服务器")
        except (OSError, ssl.SSLError, smtplib.SMTPException):
            if submitting:
                return SendOutcome(
                    "unknown", "submission_uncertain", "提交结果不确定，请检查发件箱"
                )
            return SendOutcome("failed", "network", "无法连接邮箱服务器")
        except (KeyError, TypeError, ValueError):
            return SendOutcome("failed", "config", "邮箱发送配置无效")

        return SendOutcome(
            "accepted",
            "server_accepted",
            "邮箱服务器已接受，不代表客户已收到或阅读",
            str(job.get("message_id", "")),
        )

    def test_connection(self, config: dict, secret: str) -> SendOutcome:
        """Authenticate over encrypted SMTP without submitting a message."""
        address = str(config.get("address", "")).strip()
        if not address or not secret:
            return SendOutcome("failed", "auth", "邮箱账号或授权凭据未配置")
        context = ssl.create_default_context()
        try:
            with self._connect(config, context) as client:
                if config.get("transport", "ssl") == "starttls":
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                client.login(address, secret)
        except smtplib.SMTPAuthenticationError:
            return SendOutcome("failed", "auth", "邮箱认证失败，请检查授权凭据")
        except (OSError, ssl.SSLError, smtplib.SMTPException, TimeoutError):
            return SendOutcome("failed", "network", "无法连接邮箱服务器")
        except (KeyError, TypeError, ValueError):
            return SendOutcome("failed", "config", "邮箱发送配置无效")
        return SendOutcome("accepted", "connection_ok", "邮箱连接和认证成功")
