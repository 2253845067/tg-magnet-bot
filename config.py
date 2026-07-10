from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _ids(name: str) -> set[int]:
    raw = _str(name)
    if not raw:
        return set()
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            values.add(int(part))
    return values


def _csv(name: str, default: str = "") -> list[str]:
    raw = _str(name, default)
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


CILI_DEFAULT_BASE_URLS = (
    "https://xcili.net,https://1cili.net,https://cili.info,https://cili.uk,https://wuji.me"
)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_ids: set[int]
    telegram_proxy_url: str
    cili_proxy_url: str
    clouddrive_grpc_addr: str
    clouddrive_grpc_tls: bool
    clouddrive_api_token: str
    clouddrive_username: str
    clouddrive_password: str
    clouddrive_totp_code: str
    clouddrive_dest_folder: str
    clouddrive_auto_create_dest_folder: bool
    clouddrive_check_folder_after_secs: int
    cili_base_urls: list[str]
    max_search_results: int
    http_timeout_secs: int
    cili_max_retries: int
    cili_retry_backoff_secs: int
    log_level: str
    polling_timeout_secs: int
    polling_connect_timeout_secs: int
    polling_read_timeout_secs: int
    polling_max_failures: int
    polling_staleness_secs: int


def load_settings() -> Settings:
    load_dotenv()

    settings = Settings(
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_ids=_ids("TELEGRAM_ALLOWED_USER_IDS"),
        telegram_proxy_url=_str("TELEGRAM_PROXY_URL"),
        # cili scrapes overseas sites; by default route it through the same
        # proxy the bot uses for Telegram so it isn't crawling over a direct
        # (often blocked / slow) connection. Override with CILI_PROXY_URL.
        cili_proxy_url=_str("CILI_PROXY_URL", _str("TELEGRAM_PROXY_URL")),
        clouddrive_grpc_addr=_str("CLOUDDRIVE_GRPC_ADDR", "host.docker.internal:19798"),
        clouddrive_grpc_tls=_bool("CLOUDDRIVE_GRPC_TLS"),
        clouddrive_api_token=_str("CLOUDDRIVE_API_TOKEN"),
        clouddrive_username=_str("CLOUDDRIVE_USERNAME"),
        clouddrive_password=_str("CLOUDDRIVE_PASSWORD"),
        clouddrive_totp_code=_str("CLOUDDRIVE_TOTP_CODE"),
        clouddrive_dest_folder=_str("CLOUDDRIVE_DEST_FOLDER", "/"),
        clouddrive_auto_create_dest_folder=_bool("CLOUDDRIVE_AUTO_CREATE_DEST_FOLDER", True),
        clouddrive_check_folder_after_secs=_int("CLOUDDRIVE_CHECK_FOLDER_AFTER_SECS", 10),
        cili_base_urls=_load_cili_base_urls(),
        max_search_results=max(1, min(_int("MAX_SEARCH_RESULTS", 8), 20)),
        http_timeout_secs=_int("HTTP_TIMEOUT_SECS", 15),
        cili_max_retries=max(0, _int("CILI_MAX_RETRIES", 2)),
        cili_retry_backoff_secs=max(0, _int("CILI_RETRY_BACKOFF_SECS", 1)),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        polling_timeout_secs=_int("POLLING_TIMEOUT_SECS", 20),
        polling_connect_timeout_secs=_int("POLLING_CONNECT_TIMEOUT_SECS", 10),
        # read_timeout must stay comfortably above the long-poll timeout,
        # otherwise getUpdates raises ReadTimeout before Telegram responds.
        polling_read_timeout_secs=_int("POLLING_READ_TIMEOUT_SECS", 60),
        polling_max_failures=_int("POLLING_MAX_FAILURES", 5),
        polling_staleness_secs=_int("POLLING_STALENESS_SECS", 300),
    )

    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    if not settings.clouddrive_api_token and not (
        settings.clouddrive_username and settings.clouddrive_password
    ):
        raise ValueError(
            "Set CLOUDDRIVE_API_TOKEN, or set CLOUDDRIVE_USERNAME and CLOUDDRIVE_PASSWORD"
        )

    if not settings.clouddrive_dest_folder.startswith("/"):
        raise ValueError("CLOUDDRIVE_DEST_FOLDER must be an absolute CloudDrive path")

    if not settings.cili_base_urls:
        raise ValueError("At least one CILI base URL is required")

    return settings


def _load_cili_base_urls() -> list[str]:
    raw = _str("CILI_BASE_URLS")
    if raw:
        return _csv("CILI_BASE_URLS")

    raw = _str("CILI_BASE_URL")
    if raw:
        return _csv("CILI_BASE_URL")

    return _csv("CILI_BASE_URLS", CILI_DEFAULT_BASE_URLS)
