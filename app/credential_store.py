"""Fail-closed storage for outbound-email credentials."""

from __future__ import annotations

import keyring


VALID_PROVIDERS = {"qq_smtp", "gmail_smtp"}


class CredentialUnavailable(RuntimeError):
    """The system credential manager cannot provide the requested secret."""


class CredentialStore:
    def __init__(self, backend=None) -> None:
        self.backend = backend or keyring

    @staticmethod
    def _service(provider: str) -> str:
        if provider not in VALID_PROVIDERS:
            raise ValueError("不支持的邮箱提供商")
        return f"Tradewind/outreach-email/{provider}"

    @staticmethod
    def _address(address: str) -> str:
        value = address.strip().casefold()
        if not value:
            raise ValueError("邮箱地址不能为空")
        return value

    def set(self, provider: str, address: str, secret: str) -> None:
        username = self._address(address)
        value = secret.strip()
        if not value:
            raise ValueError("邮箱地址和授权凭据不能为空")
        try:
            self.backend.set_password(self._service(provider), username, value)
        except Exception:
            raise CredentialUnavailable("系统凭据管理器不可用") from None

    def get(self, provider: str, address: str) -> str:
        username = self._address(address)
        try:
            value = self.backend.get_password(self._service(provider), username)
        except Exception:
            raise CredentialUnavailable("系统凭据管理器不可用") from None
        if not value:
            raise CredentialUnavailable("邮箱授权凭据尚未配置")
        return value

    def has(self, provider: str, address: str) -> bool:
        username = self._address(address)
        try:
            return bool(self.backend.get_password(self._service(provider), username))
        except Exception:
            raise CredentialUnavailable("系统凭据管理器不可用") from None

    def delete(self, provider: str, address: str) -> None:
        username = self._address(address)
        try:
            service = self._service(provider)
            if self.backend.get_password(service, username):
                self.backend.delete_password(service, username)
        except Exception:
            raise CredentialUnavailable("系统凭据管理器不可用") from None
