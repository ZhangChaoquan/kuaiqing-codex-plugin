---
name: kuaiqing-skill-manager
description: 连接快擎 OAuth 后及开始网站管理任务时自动检查、安装和更新签名官方业务 Skills；也支持手动检查、更新及回滚。
---

# 快擎 Skill Manager

只使用本 Skill 所在插件根目录的 `scripts/skill_manager.py`，不下载或临时编写更新脚本。

## 自动维护

OAuth 连接成功后，以及每个网站管理任务开始时，运行 `python3 <插件根目录>/scripts/skill_manager.py sync`。官方业务 Skill 的安装和更新属于连接后的能力维护，不逐次请求确认，不要求重复 OAuth。公开目录不含用户数据，更新器也不接收 OAuth token。

读取 JSON 结果：`installed/updated/current` 表示可读取其 `skill_path`。当前对话根据用户任务选择并直接读取最新 `SKILL.md` 及其所需引用；新任务会自动发现已安装 Skill。不能把文件已更新说成运行中对话的旧指令已经自动替换。

`failed` 必须说明原因，不可声称更新成功；所需 Skill 更新失败时暂停依赖它的操作。已有手工 Skill、局部修改、坏签名、摘要不符或不兼容版本均不覆盖。管理员发布 Skill 不自动安装；回滚固定的版本不自动升级，目录降级只报告 `rollback_available`。不因目录下架而删除本地文件或网站数据。

## 手动检查、更新和回滚

- `check [skill-id]`：查询版本与发布说明，无写入。
- 用户明确要求手动安装或更新时：先 check，使用明确版本运行 `install <skill-id> <version> --confirmed` 或 `update`。已有明确指令无需重复确认；该命令会替换目标目录，检测到手工修改时先说明精确影响。
- 仅在用户明确要求回滚时运行 `rollback <skill-id>`；保留一个上一版，并固定回滚后的版本。明确手动 update 可解除固定。

签名、摘要、兼容性、ZIP 白名单或原子替换失败均保留当前版本并报告；不得转用平台 SSH 或数据库代做网站业务。
