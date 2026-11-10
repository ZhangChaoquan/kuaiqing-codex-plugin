# 快擎 Codex 插件

快擎官方 Codex 插件源码仓库。`main` 保存可编辑源码，`release` 只由快擎 SaaS 发布服务生成。

正式版使用 `https://my.kuaiqingweb.com` 的 OAuth MCP 与签名 Skills Catalog。

## 本地验证

```bash
python3 -m unittest discover -s tests
python3 /Users/laozhang/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/kuaiqing
python3 /Users/laozhang/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/kuaiqing/skills/kuaiqing-account-connection
python3 /Users/laozhang/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/kuaiqing/skills/kuaiqing-skill-manager
```

## 正式安装

```bash
codex plugin marketplace add https://github.com/ZhangChaoquan/kuaiqing-codex-plugin.git --ref release
codex plugin add kuaiqing@kuaiqing
```

Marketplace 更新或插件安装后，新建 Codex 任务加载插件入口。OAuth 连接成功后及每个网站管理任务开始时，插件 Skill 自动运行 `skill_manager.py sync`，安装或更新官方业务 Skills；当前对话直接读取返回的最新 Skill 文件。更新继续校验签名、摘要与兼容性，不覆盖手工 Skill 或本地修改。业务发布确认与 Skill 更新分开，平台维护对话只补能力。
