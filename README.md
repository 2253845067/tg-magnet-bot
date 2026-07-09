# Telegram 磁力离线下载机器人

这个项目是一个 Telegram 机器人：你把关键词发给机器人，它会搜索配置好的磁力站点；你手动选择搜索结果后，机器人会提取磁力链接，并通过 CloudDrive2 官方 gRPC API 的 `AddOfflineFiles` 方法提交离线下载任务。

请只用于你有权访问和保存的内容。

## 功能

- 发送普通文本关键词，返回可点击的磁力搜索结果。
- 手动选择某一条结果后提交到 CloudDrive2。
- 支持直接发送 `magnet:?xt=...` 磁力链接入库。
- 支持多个磁力站备用域名，遇到 403/5xx 会自动尝试下一个。
- 支持 Docker / Docker Compose 部署。
- 支持 CloudDrive2 API Token 或账号密码登录。
- 支持目标目录不存在时自动创建。

## 准备

1. 在 BotFather 创建 Telegram 机器人，获取 `TELEGRAM_BOT_TOKEN`。
2. 确认 CloudDrive2 正在运行，并且目标网盘支持离线下载。
3. 推荐创建 CloudDrive2 API Token，至少授予：

```text
allow_add_offline_download
```

如果需要机器人自动创建目标目录，还需要：

```text
allow_create_folder
```

## 配置

复制示例配置：

```bash
cp .env.example .env
```

然后编辑 `.env`，至少填写：

```env
TELEGRAM_BOT_TOKEN=123456:replace-me
CLOUDDRIVE_API_TOKEN=your-clouddrive-api-token
CLOUDDRIVE_GRPC_ADDR=192.168.1.2:19798
CLOUDDRIVE_GRPC_TLS=false
CLOUDDRIVE_DEST_FOLDER=/115open/cili
CLOUDDRIVE_AUTO_CREATE_DEST_FOLDER=true
CILI_BASE_URLS=https://xcili.net,https://1cili.net,https://cili.info,https://cili.uk,https://wuji.me
```

### Telegram 代理

如果你的网络需要代理才能访问 Telegram，推荐只给 Telegram Bot API 配置代理，不要给整个容器设置 `HTTP_PROXY`，避免 CloudDrive2 gRPC 也被代理影响。

host 网络模式下，如果 Clash 运行在宿主机，通常填：

```env
TELEGRAM_PROXY_URL=http://127.0.0.1:7890
```

默认 bridge 网络下，可以尝试：

```env
TELEGRAM_PROXY_URL=http://host.docker.internal:7890
```

这里建议使用 Clash 的 HTTP/mixed 端口。端口号按你的 Clash 实际配置修改，常见是 `7890`。

### CloudDrive2 地址

`CLOUDDRIVE_GRPC_ADDR` 应该填写 CloudDrive2 原生 gRPC 地址，优先使用直连 `host:port`：

```env
CLOUDDRIVE_GRPC_ADDR=192.168.1.2:19798
CLOUDDRIVE_GRPC_TLS=false
```

如果 CloudDrive2 就运行在 Docker Desktop 宿主机上，也可以尝试：

```env
CLOUDDRIVE_GRPC_ADDR=host.docker.internal:19798
CLOUDDRIVE_GRPC_TLS=false
```

不建议把普通网页反代地址填给 `CLOUDDRIVE_GRPC_ADDR`。gRPC 对 HTTP/2、TLS、压缩头都比较敏感，普通 HTTP 反代很容易出现 `gzip`、TLS 握手失败、`Connection reset by peer` 等问题。

### Lucky 反代说明

如果必须使用 Lucky：

- 推荐使用 TCP 转发，直接转发到 CloudDrive2 原生 gRPC 端口。
- 不要把后端写成普通网页反代，例如 `http://192.168.1.2:19798`。
- 如果后端是 CloudDrive2 原生 gRPC 明文端口，Lucky 后端“使用安全连接”通常不要开启。
- `CLOUDDRIVE_GRPC_TLS=true` 只适用于机器人连接到 Lucky 入口本身是 HTTPS/gRPC TLS 的情况。
- 关闭 gzip、压缩、缓存等会改写 gRPC 请求的功能。

如果遇到这些错误：

```text
grpc: Decompressor is not installed for grpc-encoding "gzip"
tls: first record does not look like a TLS handshake
Connection reset by peer
```

优先改用原生 gRPC 直连地址测试：

```env
CLOUDDRIVE_GRPC_ADDR=192.168.1.2:19798
CLOUDDRIVE_GRPC_TLS=false
```

### 限制 Telegram 使用者

建议填写你的 Telegram 数字用户 ID：

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

多个用户用英文逗号分隔。留空表示所有能找到机器人的人都能使用。

## Docker 运行

默认 bridge 网络运行：

```bash
docker compose up -d --build
docker compose logs -f
```

如果 CloudDrive2 在局域网机器上，比如 `192.168.1.2:19798`，默认 bridge 网络通常也能访问，不一定需要 host 模式。

## Host 网络模式测试

如果你想让容器尽量贴近宿主机网络，可以使用单独的 host 模式 compose 文件：

```bash
docker compose -f docker-compose.host.yml up -d --build
```

使用前先在 `.env` 里配置直连地址：

```env
CLOUDDRIVE_GRPC_ADDR=192.168.1.2:19798
CLOUDDRIVE_GRPC_TLS=false
```

Docker Desktop 使用 `network_mode: host` 前，需要先在 Docker 设置里启用 Host networking。没有启用时，请使用默认 `docker-compose.yml`。

## 常用命令

- 发送任意文本：搜索磁力并返回可选择结果。
- `/search 关键词`：搜索磁力并返回可选择结果。
- 点击搜索结果按钮：提交对应磁力到 CloudDrive2。
- 发送 `magnet:?xt=...`：直接提交磁力链接。
- `/status`：检查 CloudDrive2 gRPC 连接状态。

## 排查

查看容器日志：

```bash
docker compose logs -f
```

确认容器实际读取到的 CloudDrive2 配置：

```bash
docker compose exec tg-magnet-bot python -c "import os; print(os.getenv('CLOUDDRIVE_GRPC_ADDR'), os.getenv('CLOUDDRIVE_GRPC_TLS'))"
```

如果使用 host 模式：

```bash
docker compose -f docker-compose.host.yml exec tg-magnet-bot python -c "import os; print(os.getenv('CLOUDDRIVE_GRPC_ADDR'), os.getenv('CLOUDDRIVE_GRPC_TLS'))"
```

如果输出仍是旧域名或 Lucky 反代地址，说明 `.env` 没改到当前项目目录，或容器没有重新创建。

修改 `.env` 后建议重建：

```bash
docker compose down
docker compose up -d --build
```

## CloudDrive2 API 来源

项目中的 `clouddrive.proto` 来自 CloudDrive2 官方 API 文档：

- 指南：https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html
- Proto：https://www.clouddrive2.com/api/clouddrive.proto

Docker 构建时会根据 `clouddrive.proto` 生成 Python gRPC 代码。
