# AI 订阅价格监控 — 智能体指导

## 项目概览

监控多个 AI API 提供商的模型定价变动，以及多个榜单的排名/分数变动。每天拉取最新数据，与本地快照对比，发现变动时生成报告并弹出 macOS 通知。**不会自动改写提供商的价格条目**，由人工确认后更新；脚本可以刷新 `data.json` 的 `openrouterRefMin` 和 `updateDate`。

**价格监控**的提供商：
- **OpenRouter**：公开 API，精确美元价格
- **apifun**：公开 API 拿分组倍率，推算实际价格（价格 = 官方美元价 × 倍率）。域名 2026-09-14 由 `apikey.fun` 改为 `apikey.fan`（旧域名已停止解析）
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
| `模型宣称与价格核查手册.md` | 判断手册 | 宣称可信度评估、价格记录核验 SOP、是否落库的决策表 |

## 关键架构不变量

- **基线用众数（mode），不用均值** — 均值会产生实际不存在的虚拟价格
- **最低参考价** 存单提供商原始综合单价，不跨提供商拼凑虚拟价
- **峰谷校准** 通过比率过滤自动处理（2x/0.5x 容差），避免非线性提供商的峰谷切换误报；**该容差仅对官方定价确有峰谷两档（`officialPrices` 含 `peakInput`）的模型启用**，否则会把"恰好 2 倍"的真实调价（如限时促销结束）静默漏报（2026-09-11 修复）
- **空响应保护** V3 快照空响应不覆盖，防止下次全量误报
- **历史文件保护** price_history.json 损坏时中止执行，不静默清空
- **原子写入** 所有 JSON 写入先写临时文件再原子重命名，防止写入中断导致文件损坏
- **仅内容变化时写入** data.json 通过 diff 对比，仅在内容实际变化时才写入
- **真直连** `no_proxy_providers` 的直连分支必须显式传空 `ProxyHandler({})`。`build_opener()` 会自动读取 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量，使该列表与「直连失败才走代理」的判断全部失效——症状是日志里出现 `Tunnel connection failed: 502` 这类"直连"却报代理错误（2026-09-14 修复）
- **舍入容差要带 EPS** ¥0.005 绝对差容差用于吸收提供商页面两位小数的舍入，但 `abs(0.08-0.075)` 在二进制浮点下是 `0.0050000000000000044 > 0.005`，会让"恰好舍入 0.005"这一最常见情形漏过容差、产生永久误报。比较必须写成 `<= 0.005 + 1e-9`（2026-09-14 修复）
- **报告目录已存在时不要再 mkdir** 沙箱/权限代理会把 `mkdir(exist_ok=True)` 误判为 EEXIST 抛 `PermissionError`，导致历史已写入而报告丢失、脚本 traceback 退出。先 `exists()` 判断（2026-09-14 修复）
- **officialPrices 是唯一官方价数据源**，单位是**人民币**（= 官方美元价 × `fx_rate`）。apifun 的推算口径是 `officialPrices / fx_rate × 分组倍率`（即官方美元价 × 倍率）。录入前务必与 OpenRouter `/api/v1/models` 的 `pricing.prompt` × fx 交叉核验：2026-09-14 发现 `gemini-3.8-flash` 的 officialPrices 比真实官方价高 2 倍（10.5 vs 5.25），已按 OpenRouter 与 apifun 页面双重佐证修正

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
- **验证按改动范围选择**：文档修改检查内容和引用即可；逻辑修改优先运行针对性离线测试。`python3 check_prices.py --verbose` 是联网集成检查，会写快照、历史、报告和部分 `data.json` 字段并可能触发通知，不是无副作用的测试。需要该检查时，在授权范围和环境允许的条件下执行，并检查输出中的提供商错误，不能仅以退出码判定通过。未运行或受阻时明确说明验证缺口。
- **文档同步**：行为或工作流变化时更新 `价格监控指南.md`
- **价格更新授权**：默认只检测并报告；只有用户明确授权了具体价格变更时才编辑 `data.json` 的价格条目，不需要对同一变更重复确认。授权不明或价格来源不可靠时先澄清。
- **改价格前先核验真伪**：脚本对 input / output 分别取众数再拼合，可能产出无 provider 真实提供的虚拟价。改之前按 `模型宣称与价格核查手册.md` 的 SOP-1 确认该 (input, output) 配对真实存在；遇众数平票先确认口径再改。
- **新模型宣称先评估再录入**：厂商/社区宣称「媲美旗舰」时，按手册的四问清单与三层结构性反证评估，不采信无参数表、无跨级对比、或源自问卷题目的说法；限时内测版不录入，等正式发布。
- **不要**：将 `.local.json` 文件提交到 git
