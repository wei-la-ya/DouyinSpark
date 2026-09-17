# DouyinSpark 外置配置服务

把「添加/修改账号」的配置网页（扫码登录 + 会话列表点选目标）部署到独立服务器，适合 Core 不在公网的场景。插件核心通过 **HTTP start + WebSocket listen** 回调拿结果（无密钥签名，WS 即可）。

## 运行

```bash
cd plugins/DouyinSpark/external
pip install fastapi uvicorn httpx
uvicorn main:app --host 0.0.0.0 --port 8080
```

服务复用插件仓库内的协议核心模块（`DouyinSpark/utils/`，不依赖 gsuid_core），因此需要和插件代码放在同一份仓库检出里。

## 插件侧配置

Web 控制台 → DouyinSpark 配置 → **外置配置服务地址** 填服务地址（如 `https://dyspark-login.example.com`），保存后 `dy添加账号` / `dy添加好友` / `dy修改账号` 自动走外置流程。

## 协议（如需自行实现服务端）

| 端点 | 说明 |
|---|---|
| `POST /dyspark/start` | body: `{auth, user_id, bot_id, account_id, initial}`，返回 `{ok, page_url}` |
| `GET /dyspark/i/{auth}` | 配置页面 |
| `POST /dyspark/api/scan/start|refresh/{auth}`、`GET /dyspark/api/scan/status/{auth}`、`POST /dyspark/api/scan/sms/{auth}` | 扫码登录 |
| `POST /dyspark/api/conversations/{auth}` | 拉取会话列表 |
| `POST /dyspark/api/setup/{auth}` | 保存（终态） |
| `WS /dyspark/ws/{auth}` | 状态推送：`{status: pending/success/failed/expired, msg, payload?}`，success 时 payload 带完整账号数据 |

## 安全提示

- 本服务无鉴权（按需求设计），请仅在内网/受信任环境暴露，或用反代加访问控制
- Cookie 明文经过 HTTP 传输，公网部署务必套 HTTPS
