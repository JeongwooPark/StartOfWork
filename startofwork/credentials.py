"""Windows Credential Manager(keyring) 기반 비밀번호 저장."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE_NAME = "StartOfWork"


def credential_target_for_url(attendance_url: str) -> str:
    """근태 URL 호스트 기반 credential_target 생성."""
    host = (urlparse(str(attendance_url or "").strip()).hostname or "").strip()
    if not host:
        host = "default"
    return f"StartOfWork:{host}"


def set_password(credential_target: str, password: str) -> None:
    target = str(credential_target or "").strip()
    if not target:
        raise ValueError("credential_target이 비어 있습니다")
    keyring.set_password(SERVICE_NAME, target, str(password or ""))


def get_password(credential_target: str) -> Optional[str]:
    target = str(credential_target or "").strip()
    if not target:
        return None
    try:
        value = keyring.get_password(SERVICE_NAME, target)
    except KeyringError:
        logging.exception("Credential Manager 조회 실패: target=%s", target)
        return None
    if value is None:
        return None
    text = str(value)
    return text if text else None


def delete_password(credential_target: str) -> None:
    target = str(credential_target or "").strip()
    if not target:
        return
    try:
        keyring.delete_password(SERVICE_NAME, target)
    except PasswordDeleteError:
        pass
    except KeyringError:
        logging.exception("Credential Manager 삭제 실패: target=%s", target)
