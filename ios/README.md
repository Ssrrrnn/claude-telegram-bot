# 小家 · iOS 首版草稿

原生 SwiftUI App，最低 iOS 18。名字与视觉暂定为「小家」、暖白与酒红，可以继续修改。

## 已准备的功能

- 小家首页、文字聊天、现有 QQ/Telegram 共用的最近聊天记录。
- 复用服务端已有的语音策略与音色，收到语音后点击播放。不额外购买语音服务。
- 查看现有日记与收藏。
- HTTPS 连接；独立连接密钥写入 iPhone Keychain，Claude/Fish/数据库密钥不会进入 App。
- 断线保留待发送消息，重试使用同一 UUID。服务端持久化去重；重试语音回复只回文字，不重复合成。

首版不含语音输入、实时电话、桌面组件、健康读取、系统级后台推送。App 在前台每 15 秒刷新已有记录，不产生新的模型调用。主动消息继续发到 QQ/Telegram；不切换原有主动消息投递端。

## 当前验证状态

12 项新增 API 测试已通过；连同 QQ、时间、主动消息、语音、衔接与伴侣功能回归，共 102 项通过。Python 编译、Shell 语法与 YAML 解析检查通过。
本地环境没有 Xcode/Swift，**尚未编译 SwiftUI、生成 IPA、签名或完成真机测试**。
生产接口尚未启用；本次代码只在开发分支保存，不触发主分支部署。

## 免费打包方案

现有 bot 仓库是私有仓库，保持私有。免费打包使用**单独的公开客户端仓库**，只复制 `ios/` 目录，不复制 bot 代码、数据库、角色提示词、聊天记录或任何密钥。

GitHub 标准托管 runner 在公开仓库免费；私有仓库则使用账号免费分钟额度并可能产生超额费用：
https://docs.github.com/en/actions/reference/runners/github-hosted-runners

本仓库没有自动触发的 iOS workflow。`build-ios.yml.example` 只是模板，且设置了公开仓库检查。公开客户端仓库的创建与公开范围需先确定，然后才运行打包：

1. 新建公开客户端仓库，只放 `ios/`。
2. 将模板复制为该仓库 `.github/workflows/build-ios.yml`。
3. 手动运行 workflow，下载 `Companion-unsigned` artifact，其中是未签名 IPA。
4. iPhone「文件」保存 IPA，AltStore → My Apps → `+` → 选择 IPA，以自己的免费 Apple 账号签名安装。

所有构建命令见 `build.sh`。Apple 账号密码和签名证书不交给 GitHub；签名由用户电脑上的 AltServer 完成。无 Mac 购买、云 Mac 租赁或 Apple 开发者年费；已有后端与模型/语音的使用费用仍按现有服务结算。

## 后端接入（待部署）

后端运行在现有 bot 进程中，不创建新 Railway 服务。

1. 合并经过审阅的后端修改，安装新增的 `aiohttp` 依赖。
2. 为 bot 设置独立的 `IOS_APP_TOKEN`（至少 32 字符随机值，例如 `python -c "import secrets; print(secrets.token_urlsafe(32))"`）。不要复用 Telegram token 或 Claude/Fish key。
3. 为当前 bot 生成 HTTPS Railway 域名，路由端口使用服务的 `PORT`（未设时 8080）。只公开带鉴权的 App 接口，不公开 Postgres/NapCat。
4. App 设置填写这个 HTTPS 地址和独立连接密钥，点击「保存并连接」。

未设置 `IOS_APP_TOKEN` 时接口不启动。固定会话由 `TELEGRAM_OWNER_ID` 或既有已绑定所有者决定，客户端无法指定其他用户。修改密钥可以撤销旧 App 的访问。

接口：`GET /health`、`GET /v1/history`、`POST /v1/chat`、`GET /v1/favorites`、`GET /v1/diaries`。
私有接口都要求 `Authorization: Bearer <IOS_APP_TOKEN>`，并返回 `Cache-Control: no-store`。

聊天正文是 `{ "request_id": "UUID", "text": "消息", "sent_at": 1791115200 }`；`sent_at` 为实际发送时的 Unix 秒，接受一周内的待发送消息。处理中的重试返回 409；其他同时发送的消息返回 429。进程中断留下的请求不会自动重新生成，应先查看已有聊天记录再决定是否发送新消息。

音频仅在当前运行的 App 内存中保留，重启 App 后仍能查看文字历史。持久化请求收据保留文字，不保存音频。

## 本地 API 检查

```sh
python -m unittest test_ios_api -v
python -m py_compile ios_api.py runner.py runner_20m.py
```

这些检查不连接生产数据库，也不调用 Claude/Fish 或 Telegram。实际 PostgreSQL、线上部署、iOS 编译和真机交互需要下一步验证。
