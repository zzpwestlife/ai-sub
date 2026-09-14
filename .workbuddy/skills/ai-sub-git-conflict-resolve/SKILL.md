---
name: ai-sub-git-conflict-resolve
description: 解决 ai-sub 仓库的 git 冲突。当 git status 出现 UU / both modified（常见于 data.json、price_history.json、check_prices.py），或用户说「fix conflicts」「解决冲突」「合并冲突」时使用。核心是判定 ours/theirs 各对应哪条工作线，再按文件类型（价格数据 / 追加型日志 / 代码）分别决策，而不是整份取一侧。
agent_created: true
---

# ai-sub 冲突解决 SOP

本仓库存在**多条并行工作线**（不同工作目录的会话各写各的），冲突两侧往往**互有新旧、互有独占内容**，
不存在"取一边就完事"的默认答案。先做诊断，再逐文件决策。

## Step 1 — 判定冲突来源与 ours/theirs 语义

```bash
git status --short --branch          # UU = both modified
git ls-files -u                      # 三阶段 blob：1=base 2=ours 3=theirs
ls .git/MERGE_HEAD .git/rebase-merge .git/rebase-apply .git/logs/refs/stash
git stash list; git rev-parse stash
```

标签对照（工作区冲突标记里也能看到）：

| 标记 | 阶段 | 含义 |
|------|------|------|
| `<<<<<<< Updated upstream` | stage 2 = **ours** | 当前 HEAD |
| `>>>>>>> Stashed changes` | stage 3 = **theirs** | 被 pop/apply 的 stash |
| `>>>>>>> HEAD` / 分支名 | stage 3 | merge / rebase 场景 |

**先验证前提**：`git rev-parse :2:<file>` 是否等于 `HEAD:<file>`。相等则 ours 就是 HEAD，
可用 `git show HEAD:<file>`、`git show :3:<file>` 直接取两侧完整内容比对（比读 conflict marker 可靠得多）。

⚠️ **不要假设"stash 一定比 HEAD 新"**。本仓库曾出现 stash 的 `updateDate` 是 09-12、
而 HEAD 是 09-14 的情况——stash 反而是旧的分支级 WIP。

## Step 2 — 判定哪条线更新/更可信

对代码和数据文件分别找"时间戳锚点"：

- `data.json` → `updateDate`
- `check_prices.py` → 硬编码常量（`apikey.fan` vs 已停解析的 `apikey.fun`）、
  `AGENTS.md` 里列出的不变量
- `.workbuddy/memory/*.md` 与 `.workbuddy-ai/memory/*.md` → 各工作线的会话日志，
  **同时存在当天的两份日志就说明有两条并行工作线**
- `AGENTS.md`、`价格监控指南.md` → 行为描述是否与代码一致

## Step 3 — 逐文件决策（关键）

| 文件 | 口径 |
|------|------|
| `data.json` | **取 `updateDate` 更新的那一侧整份**。价格数据是"事实快照"，不能拼接：旧侧的官方价可能已被证伪（例：`gemini-3.8-flash` 的 officialPrices 10.5 是错的，正确值 5.25） |
| `price_history.json` | **追加型日志，先做集合比对再决定**：忽略 `time`/`note`，按 `(provider, model, field, from, to)` 计数。若一侧的所有记录都能在另一侧找到同内容条目（另一侧是超集），**取覆盖更全的那侧**；只有两侧各有独有事件时才做并集。机器写入、行序敏感，整份替换比手工删行安全 |
| `check_prices.py` | **按改动语义逐块合并，两边都要**。修复类（`ProxyHandler({})`、`0.005 + 1e-9`、`mkdir` 前置 `exists()`、峰谷容差的 `officialPrices` 门控、`apikey.fan`）与功能类（Cubence 可用性监控、`CUBENCE_ID_MAP`、分组改名注释）互不冲突 |
| `check_leaderboard.py` / `leaderboard_data.json` / `*.md` | 通常能自动合并；只确认无残留标记 |
| `.local.json` | 已 gitignore，不参与冲突；但它是 `DEFAULT_CONFIG` 的**事实来源**，可用它校验自动合并结果对不对 |

## Step 4 — 必做验证

```bash
grep -rn -E '^(<<<<<<< |>>>>>>> |=======$)' --include='*.py' --include='*.json' --include='*.md' .   # 0 命中
python3 -m py_compile check_prices.py
python3 -c "import json;[json.load(open(f)) for f in ('data.json','price_history.json')]"
python3 test_nonelinear_peakguard.py check_prices.py     # 必须 3/3 PASS
git diff --stat HEAD -- <file>                            # 复核净改动=预期
```

> `test_nonelinear_peakguard.py` 是**冲突解决的关键哨兵**：它对"峰谷门控被回滚"的版本会
> 在用例 1（GLM 促销结束 2 倍）FAIL。冲突后必须跑，别只看 py_compile。

## 陷阱（血泪）

1. **不要用 `git checkout --theirs -- file` 整份取一侧来"解决"代码冲突**——会静默回滚另一侧的
   功能或修复，且 `git status` 会显示干净，问题要到下次运行才暴露。
2. **自动合并也会引入回退**：只有一侧改动的区域，git 直接采纳那侧。若那侧是较旧的工作线，
   旧值会悄悄覆盖新值。必须逐条检查 `git diff HEAD -- file` 里非自己预期的改动。
   实例：stash 把 `no_proxy_providers` 里的 `apifun` 去掉（09-12 的结论），
   09-14 的 HEAD 没碰过这行 → git 采纳 stash 版本。最终以 `monitor_config.local.json`
   的实际配置为准（该文件是 no_apifun + 端口 7898，与 stash 一致）。
3. **`git diff :2: :3:` 里 `-` 是 ours（HEAD），`+` 是 theirs（stash）**，方向极易读反；
   判断"谁有这行"时优先用 `git show :N:file | grep` 直接验证，不要靠读 diff 猜。
4. `README`/文档类冲突解决后要回头核对与代码是否仍然一致（本仓库文档写到了 `lb-*` class、
   `CUBENCE_ID_MAP` 这类实现细节）。
5. 冲突解决只应 `git add`（标记已解决），**不要顺手 commit**——是否提交由用户决定。
