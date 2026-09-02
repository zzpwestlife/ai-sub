# AI 订阅价格监控 — 智能体指导

## 项目概览

监控多个 AI API 提供商的模型定价变动，以及多个榜单的排名/分数变动。每天拉取最新数据，与本地快照对比，发现变动时生成报告并弹出 macOS 通知。**不会自动修改 data.json**，由人工确认后手动更新。

**价格监控**的提供商：
- **OpenRouter**：公开 API，精确美元价格
- **apifun**：公开 API 拿分组倍率，推算实际价格（价格 = 官方美元价 × 倍率）
- **V3 API**：公开 API 拿基础倍率（分组倍率不公开，用快照对比检测变动）
- **非线智能**：公开 /models 接口，直接返回人民币价格
- **AIHubMix**：公开 API，从 ratio 还原美元价格

**榜单追踪**的来源：
- **aihot 综合榜**：追踪 data.json 中 12 个模型的排名/得分（Next.js SSR payload）
- **aihubmix 排行榜**：追踪 Overall + Coding 两个榜单的全量模型（公开 JSON API，含价格/延迟）

## 核心源文件

| 文件 | 角色 | 说明 |
|------|------|------|
| `check_prices.py` | 价格监控主脚本 | 价格拉取、对比、报告生成、macOS 通知 |
| `check_leaderboard.py` | 榜单追踪脚本 | aihot + aihubmix 榜单抓取、diff、通知 |
| `index.html` | 看板页面 | 价格对比可视化，综合单价排序渲染 |
| `data.json` | 核心价格数据 | 各提供商模型定价 + openrouterRefMin 最低参考价 |
| `leaderboard_data.json` | 榜单快照数据 | aihot + aihubmix 每日快照（命名空间格式） |
| `price_history.json` | 价格历史记录 | 价格变动时间线，损坏时中止执行而非静默清空 |
| `monitor_config.local.json` | 本地配置 | 代理、汇率、变动阈值（已 gitignore） |
| `secrets.local.json` | 密钥 | API 密钥等敏感信息（已 gitignore） |
| `价格监控指南.md` | 操作文档 | 详细使用说明 |

## 关键架构不变量

- **基线用众数（mode），不用均值** — 均值会产生实际不存在的虚拟价格
- **最低参考价** 存单提供商原始综合单价，不跨提供商拼凑虚拟价
- **峰谷校准** 通过比率过滤自动处理（2x/0.5x 容差），避免非线性提供商的峰谷切换误报
- **空响应保护** V3 快照空响应不覆盖，防止下次全量误报
- **历史文件保护** price_history.json 损坏时中止执行，不静默清空
- **原子写入** 所有 JSON 写入先写临时文件再原子重命名，防止写入中断导致文件损坏
- **仅内容变化时写入** data.json 通过 diff 对比，仅在内容实际变化时才写入

## 运行方式

```bash
python3 check_prices.py            # 正常检查
python3 check_prices.py --verbose  # 打印详细过程
python3 check_prices.py --note "说明"  # 附加备注到本次检测的历史条目
```

无需安装依赖，仅使用 Python 标准库。

## 配置模式

- `monitor_config.local.json`：代理地址、美元兑人民币汇率（默认 7.0）、变动阈值百分比（默认 0.5%）
- 综合单价公式：70%×有效输入 + 30%×输出，命中率默认 90%
- 所有 `.local.json` 文件已 gitignore，不提交到仓库

## 范围边界

- **修改价格逻辑时**：先改 `check_prices.py`，再同步更新 `index.html` 渲染（如适用）
- **修改后**：运行 `python3 check_prices.py --verbose` 验证无报错
- **文档同步**：行为或工作流变化时更新 `价格监控指南.md`
- **不要**：直接编辑 `data.json` 的价格数据（应由脚本检测 + 人工确认）
- **不要**：将 `.local.json` 文件提交到 git
