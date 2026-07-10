from __future__ import annotations

import logging

from bot import MagnetBot
from cili import CiliClient
from clouddrive_client import CloudDriveClient
from config import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cili = CiliClient(
        settings.cili_base_urls,
        settings.http_timeout_secs,
        max_retries=settings.cili_max_retries,
        retry_backoff_secs=settings.cili_retry_backoff_secs,
        proxy_url=settings.cili_proxy_url,
    )
    clouddrive = CloudDriveClient(
        settings.clouddrive_grpc_addr,
        use_tls=settings.clouddrive_grpc_tls,
        api_token=settings.clouddrive_api_token,
        username=settings.clouddrive_username,
        password=settings.clouddrive_password,
        totp_code=settings.clouddrive_totp_code,
        auto_create_dest_folder=settings.clouddrive_auto_create_dest_folder,
        check_folder_after_secs=settings.clouddrive_check_folder_after_secs,
    )

    bot = MagnetBot(settings, cili, clouddrive)
    app = bot.build_application()
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        # Short long-poll timeout so the client re-opens the connection
        # regularly, avoiding proxy idle-timeout on the tunnel.
        timeout=settings.polling_timeout_secs,
    )


if __name__ == "__main__":
    main()
