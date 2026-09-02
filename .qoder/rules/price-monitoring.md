---
description: 修改 check_prices.py 或 index.html 时自动应用。包含价格监控系统的核心架构不变量、数据写入保护和验证流程。
globs: "*.py, *.html"
---

# 价格监控系统规则

修改 `check_prices.py` 或 `index.html` 时必须遵守以下不变量。

## 项目架构概览

- **唯一源文件**：`check_prices.py`（价格拉取、对比、报告生成、macOS 通知）
- **看板页面**：`index.html`（价格对比可视化，综合单价排序渲染）
- **核心数据**：`data.json`（各提供商模型定价 + openrouterRefMin 最低参考价）
- **历史记录**：`price_history.json`（价格变动时间线）
- **本地配置**：`monitor_config.local.json`、`secrets.local.json`（已 gitignore）
- **监控提供商**：OpenRouter（精确美元价）、apifun（分组倍率推算）、V3 API（快照对比）、非线智能（人民币直出）、AIHubMix（美元价从 ratio 还原）
- **技术栈**：纯 Python 标准库，无第三方依赖

## 价格基线

- 使用**众数（mode）**作为价格基线，不用均值。均值会产生实际不存在的虚拟价格。
- 平票时取中位数（mv 函数），消除跨次运行抖动。

## 非线性提供商处理

- `check_nonelinear()` 使用峰谷容差比率过滤（2x / 0.5x）。
- 当比率恰好为 2x 或 0.5x 时跳过，避免峰谷档位切换产生误报。

## OpenRouter 最低参考价

- `openrouterRefMin` 存储**单提供商**的原始综合单价。
- 不跨提供商拼凑虚拟价。
- 该字段仅供看板参考展示，不参与排序或最优计算。

## 综合单价计算

- 公式：70% × 有效输入价 + 30% × 输出价（命中率默认 90%）。
- 看板 Best Overview 表格按综合单价升序排列（null/undefined 推至末尾）。

## 数据写入保护

- 所有 JSON 写入使用 `atomic_write_json()`：先写临时文件，再原子重命名。
- `data.json` 仅在内容实际变化时写入（写入前 diff 对比）。
- V3 快照空响应不覆盖，防止下次全量误报。
- `price_history.json` 损坏时中止执行，不静默清空。

## 范围边界

- 修改价格逻辑：先改 `check_prices.py`，再同步更新 `index.html` 渲染（如适用）。
- 不要直接编辑 `data.json` 的价格数据（应由脚本检测 + 人工确认）。
- 不要将 `.local.json` 文件提交到 git。

## 验证

修改后运行：
```bash
python3 check_prices.py --verbose
```
确认无报错，且变动数量符合预期。
