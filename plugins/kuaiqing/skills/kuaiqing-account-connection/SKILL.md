---
name: kuaiqing-account-connection
description: 连接、检查或断开快擎 Codex OAuth 账号。用户要求登录、查看当前快擎账号、断开快擎，或网站管理任务需要检查连接和同步官方 Skills 时使用。
---

# 快擎账号连接

通过插件提供的 `kuaiqing` MCP 管理快擎 OAuth 连接，不使用 Agent Key、设备 Token 或其他认证兜底。

## 检查与连接

- 检查当前账号时调用 `kq.account.get_current`。
- 连接成功后或开始已连接网站的管理任务时，使用本插件 `scripts/skill_manager.py sync` 自动维护官方业务 Skills，并读取返回的任务相关 `skill_path`。不重复请求 OAuth 或逐项更新确认；更新失败按 Skill Manager 规则处理。
- MCP 返回需要认证时，使用 Codex 官方 OAuth 登录流程；不索取或保存用户密码、Token。

## 断开

仅在用户明确要求断开或退出快擎时执行：

1. 调用 `kq.account.disconnect_current`，确认 `server_token_revoked=true`。
2. 再执行 `codex mcp logout kuaiqing` 删除本地凭证。

服务端吊销失败时立即停止，不先删除本地凭证。服务端已吊销但本地 logout 失败时，说明服务端连接已失效，并提示用户重试本地清理。

不要把单独执行 `codex mcp logout` 描述为服务端已断开；Codex CLI 0.149.1 实测只会删除本地凭证。
