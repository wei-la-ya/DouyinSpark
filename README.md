# DouyinSpark（dy续火 · GsCore 插件）

抖音自动续火插件（GsCore / Python 版）。全链路纯 API：扫码登录、会话列表、私信发送、昵称查询均无浏览器依赖。**后台按用户 ID（sec_uid）寻址发送**，对方改名/换头像不影响送达；前台展示昵称 + 抖音号 + 头像，变更自动更新。

移植自 Yunzai 版 `douyin-id-spark`（同一作者仓库的 api 分支）。

## 安装

把本目录放到 gsuid_core 的 `plugins/` 下：

```
plugins/DouyinSpark/
```

然后安装依赖并重启 GsCore：

```bash
cd plugins/DouyinSpark
pip install httpx aiofiles aiosmtplib   # 或 pdm/uv 同步 pyproject.toml
```

## 使用流程

1. 私聊机器人 `dy添加账号` → 打开一次性网页链接
2. 网页中「扫码获取 Cookie」（纯 API，无需浏览器）或粘贴 Cookie-Editor JSON
3. 点「拉取会话列表」→ 勾选要续火的好友（头像 + 昵称 + 抖音号）→ 提交
4. `dy续火` 手动执行；定时任务默认每天 00:10（配置项「每日续火时间」）

## 命令一览

| 命令 | 说明 |
|---|---|
| `dy添加账号` / `dy账号列表` / `dy删除账号 账号名` / `dy修改账号 账号名` | 账号管理 |
| `dy添加好友 [账号名]` | 发链接拉取会话列表勾选新增目标（免重扫） |
| `dy好友列表 [账号名]` | 查看 昵称/抖音号 映射 |
| `dy删除好友 [账号名] 序号` | 删除单个目标 |
| `dy刷新昵称 [账号名]` | 批量刷新昵称映射 |
| `dy续火 [账号名\|全部]` | 手动续火（“全部”仅主人） |
| `dy续火 帮助` | 帮助 |

## 配置（Web 控制台）

启用定时、每日时间、消息模板（`{{friend}}`/`{{yiyan}}` 等占位符）、发送间隔、跳过今日已续、昵称补全上限、SMTP 邮件通知。

## 外置配置服务（可选）

Core 不在公网时，可把配置网页部署到独立服务器，插件通过 HTTP start + WebSocket listen 回调拿结果（无密钥）：

1. 部署 `external/`（见 external/README.md）
2. Web 控制台 → DouyinSpark 配置 → 填「外置配置服务地址」
3. `dy添加账号` / `dy添加好友` / `dy修改账号` 自动走外置流程

## 技术要点

- 扫码登录：抖音 PC 客户端 passport（imdesktop.douyin.com，sign/qs 签名 + 指纹编码），移植自 jumpbyte-bot
- 会话列表：imapi `get_message_by_init`（protobuf，cmd=2043，真实抓包校正）
- 发消息：imapi `message/send`（protobuf 模板 + sessionid）
- 签名：纯算法 a_bogus（cus 变体，ShilongLee/Crawler 同源）
- 续火前拉收件箱总览：selfUid 识别、会话补全、「今天已续过」自动跳过

## 风险声明

逆向接口仅供学习交流。Cookie 存于本地数据库，请保护服务器安全。高频/陌生人私信可能触发风控，请控制目标数量。
