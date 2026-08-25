---
name: kuaiqing-skill-manager
description: 检查、安装、更新或回滚快擎官方 Skills。用户明确要求管理快擎 Skills 时使用；单独检查不需要批准。
---

# 快擎 Skill Manager

只使用本 Skill 所在插件根目录的 `scripts/skill_manager.py`，不从网络下载或临时编写更新脚本。

## 检查

运行 `python3 <插件根目录>/scripts/skill_manager.py check [skill-id]`，向用户展示已安装版本、可用版本和更新说明。检查不产生批准请求。

## 安装或更新

1. 先执行 `check` 并展示目标版本与更新说明。
2. 取得用户一次明确确认。
3. 运行 `python3 <插件根目录>/scripts/skill_manager.py install <skill-id> <明确版本> --confirmed`；已安装时也可使用同参数的 `update`。
4. 成功后提示用户新建 Codex 任务加载新版本。

不得把用户对其他业务操作的确认当成 Skill 安装确认，也不得省略预期版本或 `--confirmed`。

## 回滚

仅在用户明确要求回滚时，运行 `python3 <插件根目录>/scripts/skill_manager.py rollback <skill-id>`。本地只保留一个上一版。

签名、摘要、兼容性、ZIP 白名单或原子替换任一失败时停止，并说明当前已安装版本保持不变。
