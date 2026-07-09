# Telegram Magnet Offline Bot

This bot accepts a search query in Telegram, searches configured cili sites, extracts the
selected magnet link from the detail page, and submits it to CloudDrive2 through
the official gRPC API method `AddOfflineFiles`.

Use it only for content you are legally allowed to access and store.

## Requirements

1. Create a Telegram bot with BotFather and get `TELEGRAM_BOT_TOKEN`.
2. Make sure CloudDrive2 is running and the target cloud folder supports offline downloads.
3. Prefer a CloudDrive2 API token with the minimum required permission:
   `allow_add_offline_download`.

## Configuration

Copy `.env.example` to `.env`, then fill at least:

```env
TELEGRAM_BOT_TOKEN=123456:replace-me
CLOUDDRIVE_API_TOKEN=your-clouddrive-api-token
CLOUDDRIVE_GRPC_ADDR=host.docker.internal:19798
CLOUDDRIVE_DEST_FOLDER=/target/folder
CLOUDDRIVE_AUTO_CREATE_DEST_FOLDER=true
CILI_BASE_URLS=https://xcili.net,https://1cili.net,https://cili.info,https://cili.uk,https://wuji.me
```

`CLOUDDRIVE_GRPC_ADDR` should point to the native CloudDrive2 gRPC endpoint.
Prefer direct `host:port`, for example `host.docker.internal:19798` or
`192.168.1.10:19798`. Do not use a normal web reverse proxy unless it is
explicitly configured for HTTP/2 gRPC and does not rewrite compression headers.
When `https://` is used, the bot automatically uses a TLS gRPC channel, but many
generic reverse proxies will break CloudDrive2 gRPC calls.
When `CLOUDDRIVE_AUTO_CREATE_DEST_FOLDER=true`, the API token also needs
`allow_create_folder` if the destination folder does not already exist.

### Lucky reverse proxy notes

If you use Lucky as the public entry:

- `CLOUDDRIVE_GRPC_TLS=true` only when the bot connects to Lucky through an
  HTTPS/gRPC TLS entry.
- If the error says `first record does not look like a TLS handshake`, the bot is
  using TLS but the target port is plain gRPC. Set `CLOUDDRIVE_GRPC_TLS=false`,
  or make the Lucky entry actually serve HTTPS/gRPC TLS on that port.
- If Lucky has a backend option similar to "gRPC use secure connection", keep it
  off when forwarding to the native CloudDrive2 gRPC port, unless your
  CloudDrive2 backend itself is configured for TLS.
- If the error says `Connection reset by peer`, Lucky is accepting the TCP
  connection but closing it before a valid gRPC call completes. Recheck that the
  route is gRPC/HTTP2, not a normal HTTP reverse proxy, and that the backend TLS
  option matches the CloudDrive2 gRPC port.
- Turn off gzip/compression/cache features on the gRPC reverse proxy route.

If you do not use an API token, configure account login instead:

```env
CLOUDDRIVE_USERNAME=your-account
CLOUDDRIVE_PASSWORD=your-password
CLOUDDRIVE_TOTP_CODE=
```

Set `TELEGRAM_ALLOWED_USER_IDS` to your numeric Telegram user id to restrict
access. Separate multiple ids with commas. Leaving it empty allows anyone who
can reach the bot to use it.

## Run With Docker

```bash
docker compose up -d --build
docker compose logs -f
```

If CloudDrive2 is also in Docker, set `CLOUDDRIVE_GRPC_ADDR` to the CloudDrive2
service name and port on the same Docker network, for example `clouddrive2:19798`.

## Commands

- Send any text: show selectable search results.
- `/search keywords`: show selectable search results.
- Click `离线 1`: submit the selected magnet to CloudDrive2.
- Send `magnet:?xt=...`: submit a magnet directly.
- `/status`: test the CloudDrive2 gRPC connection.

If one search domain returns 403/5xx, the bot automatically tries the next
domain from `CILI_BASE_URLS`.

## CloudDrive2 API Source

`clouddrive.proto` is downloaded from the official CloudDrive2 API docs:

- Guide: https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html
- Proto: https://www.clouddrive2.com/api/clouddrive.proto

The Docker build generates Python gRPC files from `clouddrive.proto` with
`grpcio-tools`.
