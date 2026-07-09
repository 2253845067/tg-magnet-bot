from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import grpc
from google.protobuf.empty_pb2 import Empty

import clouddrive_pb2 as pb2
import clouddrive_pb2_grpc as pb2_grpc


logger = logging.getLogger(__name__)

_GRPC_CHANNEL_OPTIONS = (
    ("grpc.default_compression_algorithm", 0),
    ("grpc.default_compression_level", 0),
)


class CloudDriveError(RuntimeError):
    pass


def _normalize_grpc_target(addr: str, use_tls: bool) -> tuple[str, bool]:
    raw = addr.strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        if parsed.path not in {"", "/"}:
            raise CloudDriveError("CLOUDDRIVE_GRPC_ADDR must not include a URL path")
        if not parsed.netloc:
            raise CloudDriveError("CLOUDDRIVE_GRPC_ADDR is missing host:port")
        return parsed.netloc, parsed.scheme == "https"

    if "://" in raw:
        raise CloudDriveError("CLOUDDRIVE_GRPC_ADDR only supports http:// or https:// schemes")
    return raw, use_tls


def _split_cloud_path(path: str) -> tuple[str, str]:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return "", ""
    parent_parts = parts[:-1]
    parent = "/" + "/".join(parent_parts) if parent_parts else "/"
    return parent, parts[-1]


def _looks_like_already_exists(message: str) -> bool:
    lowered = message.lower()
    return "exist" in lowered or "already" in lowered or "已存在" in message


def _is_duplicate_offline_task_error(exc: BaseException) -> bool:
    details = (exc.details() or "") if isinstance(exc, grpc.RpcError) else str(exc)
    return "code: 10008" in details or "任务已存在" in details


class CloudDriveClient:
    def __init__(
        self,
        addr: str,
        *,
        use_tls: bool,
        api_token: str = "",
        username: str = "",
        password: str = "",
        totp_code: str = "",
        auto_create_dest_folder: bool = True,
        check_folder_after_secs: int = 10,
    ) -> None:
        self._addr, self._use_tls = _normalize_grpc_target(addr, use_tls)
        self._api_token = api_token.strip()
        self._username = username
        self._password = password
        self._totp_code = totp_code
        self._auto_create_dest_folder = auto_create_dest_folder
        self._check_folder_after_secs = check_folder_after_secs
        self._jwt_token = ""
        self._jwt_expiration: datetime | None = None

        if self._use_tls:
            self._channel = grpc.secure_channel(
                self._addr,
                grpc.ssl_channel_credentials(),
                options=_GRPC_CHANNEL_OPTIONS,
            )
        else:
            self._channel = grpc.insecure_channel(
                self._addr,
                options=_GRPC_CHANNEL_OPTIONS,
            )
        self._stub = pb2_grpc.CloudDriveFileSrvStub(self._channel)

    def close(self) -> None:
        self._channel.close()

    def get_system_info(self):
        return self._stub.GetSystemInfo(
            Empty(),
            timeout=10,
        )

    def add_offline_file(self, magnet_url: str, dest_folder: str):
        metadata = self._auth_metadata()
        try:
            return self._add_offline_file_or_duplicate(magnet_url, dest_folder, metadata)
        except grpc.RpcError as exc:
            if self._auto_create_dest_folder and exc.code() == grpc.StatusCode.NOT_FOUND:
                logger.info("Destination folder %s was not found; creating it", dest_folder)
                self._ensure_folder_path(dest_folder, metadata)
                return self._add_offline_file_or_duplicate(magnet_url, dest_folder, metadata)
            raise

    def _add_offline_file_or_duplicate(
        self,
        magnet_url: str,
        dest_folder: str,
        metadata: tuple[tuple[str, str], ...],
    ):
        try:
            return self._add_offline_file(magnet_url, dest_folder, metadata)
        except (grpc.RpcError, CloudDriveError) as exc:
            if _is_duplicate_offline_task_error(exc):
                logger.info("Offline task already exists; treating it as success")
                return pb2.FileOperationResult(success=True)
            raise

    def _add_offline_file(
        self,
        magnet_url: str,
        dest_folder: str,
        metadata: tuple[tuple[str, str], ...],
    ):
        request = pb2.AddOfflineFileRequest(
            urls=magnet_url,
            toFolder=dest_folder,
            checkFolderAfterSecs=max(0, self._check_folder_after_secs),
        )
        result = self._stub.AddOfflineFiles(
            request,
            metadata=metadata,
            timeout=60,
        )
        if not result.success:
            raise CloudDriveError(result.errorMessage or "CloudDrive2 rejected the task")
        return result

    def _ensure_folder_path(self, folder_path: str, metadata: tuple[tuple[str, str], ...]) -> None:
        parent, folder_name = _split_cloud_path(folder_path)
        if not folder_name:
            return

        request = pb2.CreateFolderRequest(parentPath=parent, folderName=folder_name)
        try:
            result = self._stub.CreateFolder(
                request,
                metadata=metadata,
                timeout=30,
            )
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND and parent != "/":
                self._ensure_folder_path(parent, metadata)
                result = self._stub.CreateFolder(
                    request,
                    metadata=metadata,
                    timeout=30,
                )
            elif exc.code() == grpc.StatusCode.ALREADY_EXISTS:
                return
            else:
                raise

        if result.result.success:
            return

        message = result.result.errorMessage or "CloudDrive2 rejected folder creation"
        if _looks_like_already_exists(message):
            return
        raise CloudDriveError(f"自动创建目标目录失败：{message}")

    def _auth_metadata(self) -> tuple[tuple[str, str], ...]:
        token = self._api_token or self._ensure_jwt()
        return (("authorization", f"Bearer {token}"),)

    def _ensure_jwt(self) -> str:
        if self._jwt_token and self._jwt_is_valid():
            return self._jwt_token

        if not self._username or not self._password:
            raise CloudDriveError("CloudDrive2 username/password are not configured")

        request = pb2.GetTokenRequest(userName=self._username, password=self._password)
        if self._totp_code:
            request.totpCode = self._totp_code

        response = self._stub.GetToken(
            request,
            timeout=20,
        )
        if not response.success:
            raise CloudDriveError(response.errorMessage or "CloudDrive2 authentication failed")

        self._jwt_token = response.token
        if response.HasField("expiration"):
            self._jwt_expiration = response.expiration.ToDatetime(tzinfo=timezone.utc)
        else:
            self._jwt_expiration = None

        logger.info("Authenticated to CloudDrive2 at %s", self._addr)
        return self._jwt_token

    def _jwt_is_valid(self) -> bool:
        if self._jwt_expiration is None:
            return True
        return datetime.now(timezone.utc) < self._jwt_expiration
