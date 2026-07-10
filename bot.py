from __future__ import annotations

import asyncio
import logging
import os
import time

import grpc
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from clouddrive_client import CloudDriveClient, CloudDriveError
from config import Settings
from models import SearchResult
from cili import CiliClient, CiliError


logger = logging.getLogger(__name__)


class _PollingGuard:
    """Wrap the bot's ``get_updates`` so the process force-exits after the
    Telegram long-poll connection keeps failing.

    Telegram's ``getUpdates`` is a long-poll: when the request goes through a
    proxy (Clash, etc.) the tunnel can be dropped by the proxy's idle timeout,
    or the proxy host can go away overnight. python-telegram-bot retries those
    network errors forever without exiting, so the process stays "alive" but
    never receives messages again. Forcing the process to die lets the
    container manager (e.g. ``restart: unless-stopped``) restart it with a
    fresh connection.

    ``os._exit`` is used instead of ``sys.exit`` because ``sys.exit`` only
    raises ``SystemExit``, which the polling retry loop swallows and keeps
    retrying.

    The guard is a callable: ``await guard(original_get_updates, *args, **kwargs)``.
    """

    def __init__(
        self,
        *,
        max_consecutive_failures: int,
        staleness_timeout: float,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._max_failures = max_consecutive_failures
        self._staleness_timeout = staleness_timeout
        self._heartbeat_interval = heartbeat_interval
        self._consecutive_failures = 0
        self._last_success = time.monotonic()

    async def __call__(self, original, *args, **kwargs):
        try:
            result = await original(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._consecutive_failures += 1
            logger.warning(
                "Telegram polling failed (%d/%d consecutive); bot will restart if it persists",
                self._consecutive_failures,
                self._max_failures,
            )
            if self._consecutive_failures >= self._max_failures:
                logger.error(
                    "Too many consecutive polling failures (%d); forcing restart for recovery",
                    self._consecutive_failures,
                )
                self._force_exit()
            raise
        self._consecutive_failures = 0
        self._last_success = time.monotonic()
        return result

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            if time.monotonic() - self._last_success > self._staleness_timeout:
                logger.error(
                    "No successful Telegram polling for %.0fs; forcing restart for recovery",
                    self._staleness_timeout,
                )
                self._force_exit()

    @staticmethod
    def _force_exit() -> None:
        logger.error("Forcing process exit for self-healing restart")
        os._exit(1)


class _GuardedExtBot(ExtBot):
    """``ExtBot`` whose ``get_updates`` is wrapped by a ``_PollingGuard``.

    Subclassing is required because ``ExtBot`` is a ``TelegramObject`` and
    forbids assigning to its methods at runtime (``bot.get_updates = ...``
    raises ``AttributeError``). The guard is attached in ``post_init``.
    """

    _polling_guard: "_PollingGuard | None" = None

    async def get_updates(self, *args, **kwargs):
        if self._polling_guard is None:
            return await super().get_updates(*args, **kwargs)
        return await self._polling_guard(super().get_updates, *args, **kwargs)


class MagnetBot:
    def __init__(
        self,
        settings: Settings,
        cili: CiliClient,
        clouddrive: CloudDriveClient,
    ) -> None:
        self._settings = settings
        self._cili = cili
        self._clouddrive = clouddrive

    def build_application(self) -> Application:
        bot = self._build_guarded_bot()
        builder = (
            Application.builder()
            .bot(bot)
            .post_shutdown(self.shutdown)
            .post_init(self._start_polling_guard)
        )

        app = builder.build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("search", self.search_command))
        app.add_handler(CallbackQueryHandler(self.download_callback, pattern=r"^(dl:\d+|cancel)$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.message))
        app.add_error_handler(self.error_handler)
        return app

    def _build_guarded_bot(self) -> "_GuardedExtBot":
        settings = self._settings
        proxy = settings.telegram_proxy_url
        get_updates_request = HTTPXRequest(
            connect_timeout=settings.polling_connect_timeout_secs,
            read_timeout=settings.polling_read_timeout_secs,
            proxy_url=proxy or None,
        )
        request = HTTPXRequest(proxy_url=proxy or None)
        return _GuardedExtBot(
            token=settings.telegram_bot_token,
            request=request,
            get_updates_request=get_updates_request,
        )

    async def _start_polling_guard(self, app: Application) -> None:
        settings = self._settings
        guard = _PollingGuard(
            max_consecutive_failures=settings.polling_max_failures,
            staleness_timeout=float(settings.polling_staleness_secs),
            heartbeat_interval=min(
                30.0, float(settings.polling_staleness_secs) / 2
            ),
        )
        app.bot._polling_guard = guard
        self._polling_guard = guard
        asyncio.create_task(guard.watchdog())

    async def shutdown(self, _: Application) -> None:
        await self._cili.close()
        self._clouddrive.close()

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled bot error", exc_info=context.error)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        await update.effective_message.reply_text(
            "发关键词给我，我会搜索磁力站；也可以直接发送 magnet 链接提交离线下载。\n"
            "命令：/search 关键词，/status 检查 CloudDrive2。"
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.start(update, context)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        message = update.effective_message
        try:
            info = await asyncio.to_thread(self._clouddrive.get_system_info)
            user = getattr(info, "UserName", "") or getattr(info, "userName", "") or "未知"
            ready = getattr(info, "SystemReady", getattr(info, "systemReady", None))
            await message.reply_text(f"CloudDrive2 连接正常。用户：{user}，就绪：{ready}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("CloudDrive2 status check failed")
            await message.reply_text(f"CloudDrive2 检查失败：{_format_user_error(exc)}")

    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        query = " ".join(context.args).strip()
        if not query:
            await update.effective_message.reply_text("用法：/search 关键词")
            return
        await self._search_and_reply(update, context, query)

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return

        if text.startswith("magnet:?"):
            await self._submit_magnet(update, text, title="手动提交的磁力链接")
            return

        await self._search_and_reply(update, context, text)

    async def download_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not await self._allowed(update):
            return

        message = query.message
        if message is None:
            return

        if query.data == "cancel":
            searches = context.user_data.get("searches")
            if searches:
                searches.pop((message.chat_id, message.message_id), None)
            await message.edit_text("已取消。")
            return

        try:
            index = int(query.data.split(":", 1)[1])
        except (IndexError, ValueError):
            await message.reply_text("这个选择已失效，请重新搜索。")
            return

        searches = context.user_data.get("searches", {})
        results = searches.get((message.chat_id, message.message_id))
        if results is None or index < 0 or index >= len(results):
            await message.reply_text("这个选择已失效，请重新搜索。")
            return

        searches.pop((message.chat_id, message.message_id), None)

        result = results[index]
        status = await message.edit_text(f"正在获取磁力链接：{result.title}")
        try:
            detail = await self._cili.detail(result.detail_url)
            await asyncio.to_thread(
                self._clouddrive.add_offline_file,
                detail.magnet,
                self._settings.clouddrive_dest_folder,
            )
            await status.edit_text(_submitted_message(), parse_mode=ParseMode.HTML)
        except (httpx.HTTPError, CiliError, CloudDriveError, grpc.RpcError) as exc:
            logger.exception("download submission failed")
            await status.edit_text(f"提交失败：{_format_user_error(exc)}")

    async def _search_and_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        query: str,
    ) -> None:
        message = update.effective_message
        notice = await message.reply_text(f"正在搜索：{query}")
        try:
            results = await self._cili.search(query, self._settings.max_search_results)
        except (httpx.HTTPError, CiliError) as exc:
            logger.exception("cili search failed")
            await notice.edit_text(f"搜索失败：{_format_user_error(exc)}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected search failure")
            await notice.edit_text(f"搜索失败：程序内部错误：{exc}")
            return

        if not results:
            await notice.edit_text("没有找到结果。")
            return

        searches = context.user_data.setdefault("searches", {})
        searches[(notice.chat_id, notice.message_id)] = results
        if len(searches) > 30:
            searches.pop(next(iter(searches)))
        await notice.edit_text(
            _format_results(query, results),
            reply_markup=_result_keyboard(results),
            disable_web_page_preview=True,
        )

    async def _submit_magnet(self, update: Update, magnet: str, *, title: str) -> None:
        notice = await update.effective_message.reply_text("正在提交到 CloudDrive2...")
        try:
            await asyncio.to_thread(
                self._clouddrive.add_offline_file,
                magnet,
                self._settings.clouddrive_dest_folder,
            )
            await notice.edit_text(_submitted_message(), parse_mode=ParseMode.HTML)
        except (CloudDriveError, grpc.RpcError) as exc:
            logger.exception("manual magnet submission failed")
            await notice.edit_text(f"提交失败：{_format_user_error(exc)}")

    async def _allowed(self, update: Update) -> bool:
        allowed_ids = self._settings.telegram_allowed_user_ids
        user = update.effective_user
        if not allowed_ids or (user and user.id in allowed_ids):
            return True
        if update.effective_message:
            await update.effective_message.reply_text("你没有权限使用这个机器人。")
        return False


def _format_results(query: str, results: list[SearchResult]) -> str:
    return f"📥 {query} 找到了 {len(results)}个结果（选一个入库）："


def _result_keyboard(results: list[SearchResult]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(_result_button_text(item), callback_data=f"dl:{idx}")]
        for idx, item in enumerate(results)
    ]
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _result_button_text(item: SearchResult) -> str:
    size = item.size or "-"
    text = f"{size} | {item.title}"
    if len(text) <= 60:
        return text
    return text[:57].rstrip() + "..."


def _submitted_message() -> str:
    return "✅ 已提交入库"


def _format_user_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "请求磁力站超时，站点响应太慢或当前域名不稳定。请重试，或把 HTTP_TIMEOUT_SECS 调大一些。"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return f"磁力站返回 HTTP {status_code}，请稍后重试或换一个备用域名。"
    if isinstance(exc, httpx.RequestError):
        return f"请求磁力站失败：{str(exc) or exc.__class__.__name__}"
    if isinstance(exc, grpc.RpcError):
        code = exc.code().name if exc.code() else "UNKNOWN"
        details = exc.details() or str(exc)
        if "DNS resolution failed" in details:
            details = "DNS 解析失败，请检查 CLOUDDRIVE_GRPC_ADDR 是否能在容器内访问。"
        elif code == "NOT_FOUND" and "find by path" in details:
            details = "目标保存目录不存在，请检查 CLOUDDRIVE_DEST_FOLDER，或开启自动创建目录并给 API Token 增加 allow_create_folder 权限。"
        elif code == "PERMISSION_DENIED":
            details = f"权限不足：{details}"
        elif code == "INTERNAL" and "grpc-encoding" in details and "gzip" in details:
            details = "提交失败：当前 CLOUDDRIVE_GRPC_ADDR 经过的反代/入口不兼容 gRPC，返回了 gzip 解压错误。请改用 CloudDrive2 原生 gRPC 直连地址，例如 host.docker.internal:19798、内网IP:19798，或正确配置支持 HTTP/2 gRPC 且不改写压缩头的反代。"
        elif code == "INTERNAL" and ("code: 10008" in details or "任务已存在" in details):
            details = "任务已存在，重复链接已忽略。"
        elif code == "UNAVAILABLE" and "first record does not look like a TLS handshake" in details:
            details = "TLS 配置不匹配：机器人正在用安全连接连接 CLOUDDRIVE_GRPC_ADDR，但对方端口返回的是明文 gRPC。请把 CLOUDDRIVE_GRPC_TLS 改为 false，或让反代入口真正开启 HTTPS/gRPC TLS；如果 Lucky 的“gRPC 使用安全连接”指后端到 CloudDrive2，则 CloudDrive2 原生明文端口通常不要开启。"
        elif code == "UNAVAILABLE" and "Connection reset by peer" in details:
            details = "gRPC 连接被对端主动断开，通常是 Lucky 入口协议或后端协议不匹配。请确认该反代规则是 gRPC/HTTP2，不是普通 HTTP 反代；如果 Lucky 后端转发到 CloudDrive2 原生 gRPC 端口，后端“使用安全连接”通常要关闭。最稳妥是改用 CloudDrive2 原生 gRPC 直连地址，例如 host.docker.internal:19798 或 内网IP:19798。"
        elif code == "UNAVAILABLE":
            details = f"CloudDrive2 服务不可用：{details}"
        return f"{code}：{details}"
    return str(exc) or exc.__class__.__name__
