# AI 订阅价格监控

每天从多个 AI API 提供商拉取最新价格，与本地 `data.json` 基线对比，发现变动时生成报告并弹出 macOS 通知。不会自动修改 data.json，由人工确认后手动更新。

## 监控范围

| 提供商 | 数据源 | 监控方式 | 认证要求 |
|:------:|:------:|:--------:|:--------:|
| OpenRouter | 公开 API | 多 provider 报价取众数，精确美元价格 | 无需 |
| apifun | 公开 API | 分组倍率推算实际价格（¥ = 官方美元价 × 倍率） | 无需 |
| V3 API | 公开 API | 基础倍率快照对比（分组倍率不公开） | 无需（需代理） |
| 非线智能 | 公开 /models 接口 | 直接返回人民币价格 | 无需 |
| Cubence | REST API | 每个模型的人民币价格 | Bearer Token |

## 快速开始

环境要求：Python 3.8+，无第三方依赖。

```bash
git clone git@github.com:zzpwestlife/ai-sub.git
cd ai-sub

# 创建本地配置（按需修改代理端口）
cat > monitor_config.local.json << 'EOF'
{
  "proxy": "http://127.0.0.1:7897",
  "fx_rate": 7.0,
  "change_threshold_pct": 0.5
}
EOF

# 执行价格检查
python3 check_prices.py                  # 常规检查
python3 check_prices.py --verbose        # 详细输出
python3 check_prices.py --note "说明"    # 附加备注到历史条目
```

### 定时任务

```bash
# 每天 20:00 自动执行
(crontab -l 2>/dev/null; echo "0 20 * * * cd $(pwd) && /usr/bin/python3 check_prices.py >> price_check_cron.log 2>&1") | crontab -
```

## 查看看板

`index.html` 是纯前端看板页面，包含价格对比、变更历史、榜单追踪等 Tab。由于浏览器安全策略限制 `file://` 协议下的 `fetch()` 请求，需通过 HTTP 服务器访问：

```bash
python3 -m http.server 8080
# 浏览器访问 http://localhost:8080
```

## 配置说明

### monitor_config.local.json（已 gitignore）

**首次使用**：复制示例文件并修改
```bash
cp monitor_config.local.json.example monitor_config.local.json
# 编辑 monitor_config.local.json，根据实际代理端口调整

| 字段 | 类型 | 默认值 | 说明 |
|:----:|:----:|:------:|:----:|
| `proxy` | string | `""` | HTTP 代理地址（V3 API、AIHubMix 必需），留空则直连 |
| `no_proxy_providers` | array | `["OpenRouter", "apifun", "非线智能"]` | 不需要代理的提供商列表（直连更快） |
| `fx_rate` | float | `7.0` | 美元兑人民币汇率 |
| `change_threshold_pct` | float | `0.5` | 变动判定阈值（%） |

**多电脑部署**：每台电脑的代理端口可能不同，有两种方式配置：

1. **本地配置文件**（推荐）：在每台电脑上创建自己的 `monitor_config.local.json`
2. **环境变量**（适合容器化/自动化部署）：
   ```bash
   export AI_SUB_PROXY="http://127.0.0.1:8118"
   export AI_SUB_FX_RATE="7.0"
   export AI_SUB_THRESHOLD="0.5"
   python3 check_prices.py
   ```
   本地配置文件会覆盖环境变量的设置。

变动判定逻辑：相对差异 > 阈值 **且** 绝对差异 > ¥0.005，双重条件避免误报。

### secrets.local.json

存放 API 密钥（如 `cubence_api_key`）。当前所有监控均为公开接口，此文件为备用扩展。已 gitignore，绝不提交。

## 文件说明

| 文件 | 说明 |
|:----:|:-----|
| `check_prices.py` | 核心监控脚本（纯标准库） |
| `check_leaderboard.py` | 榜单追踪脚本（独立运行，抓取 aihot 综合榜） |
| `data.json` | 核心数据：各提供商模型定价、模型列表、订阅信息 |
| `index.html` | 可视化看板（纯前端，加载 data.json 渲染） |
| `price_history.json` | 价格变更时间线（自动维护，提交到 git） |
| `leaderboard_data.json` | 榜单每日快照（提交到 git） |
| `price_reports/` | 变动报告（gitignore） |
| `price_state/` | 运行时快照（gitignore） |
| `monitor_config.local.json` | 本地配置：代理、汇率、阈值（gitignore） |
| `secrets.local.json` | 敏感凭证（gitignore） |
| `价格监控指南.md` | 详细操作文档（含迁移步骤、故障排查、设计决策） |

## 注意事项

- **不要直接编辑 `data.json` 的价格数据** — 由脚本检测变动后，人工确认再手动更新
- **不要将 `.local.json` 文件提交到 git** — 已通过 `.gitignore` 排除
- **修改逻辑后验证** — 运行 `python3 check_prices.py --verbose` 确认无报错
- **文档同步** — 行为或工作流变化时同步更新 `价格监控指南.md`

详细的操作说明、新机器迁移步骤和故障排查请参考 [价格监控指南.md](价格监控指南.md)。
