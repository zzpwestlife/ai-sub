# 项目长期记忆 — ai-sub（AI 订阅价格监控）

## 仓库同步（origin: github.com/zzpwestlife/ai-sub，分支 master）

- **历史是严格线性的**，提交信息多为 `backup` / `update` / `1`。同步用 `git pull --rebase origin master`，
  不要产生 merge 提交。
- **`data.json` 是唯一高频双边改动的文件**：脚本（`check_prices.py`）刷 `openrouterRefMin` / `updateDate`，
  人工改模型定义与订阅角色。所以「pull 报 local changes would be overwritten」几乎每次都会出现，
  **先提交或 stash 再 pull**；只要两侧改的不是同一段，rebase 会自动合并，不必手工解冲突。
- **`git add -A` 在本仓库是安全的**：`.gitignore` 已挡掉 `secrets.local.json`、`*.local.json`、
  `price_reports/`、`price_state/`、`*.png`。但暂存后仍要 `git diff --cached --name-only | grep -Ei 'secret|\.local\.json'` 复核一遍。
- `.workbuddy/memory/*.md` 与 `.workbuddy-ai/memory/*.md`、`价格核查存疑项-*.md` **都是入库文件**，
  新增的 memory / 存疑项文件下次同步时一并提交即可（2026-09-22.md 就是这么处理的）。
- 会话内 `git push` 时 pre-push hook 会打印 `/bin/ps: Operation not permitted`——**这是沙箱限制，不影响推送**，
  看 `To github.com:... 40d10b7..a12bfe7 master -> master` 这行才是真结果。

## 已知坑：index.html 会被注入 data-page-node-id

**症状**：`index.html` 的 diff 突然多出几十行，内容是 `data-page-node-id="<随机串>"`，
出现在 `<html>` / `<head>` / `<meta>` / 每个 `<div>` 上（2026-09-23 实测 49 处 / 跨 40 行）。

**成因**：疑似某个可视化 HTML 编辑器打开该文件后按自己的节点树整体重写。**不是手写、不是脚本产出**。

**处置**：提交前剥离，别让它进仓库。剥离命令（正则 `\s*data-page-node-id="[^"]*"` → 空），
剥离后用「本地剥离版 vs HEAD 剥离版」再比一次，**差异应只剩真实改动**——这是确认没有误删的唯一手段。
2026-09-23 剥离后残留差异正好 4 处（`NON_ACTIVE_ROLES` + 3 处连带改动）。

**注意**：剥离前先问用户一句。万一那是他在用的编辑器状态，删了会破坏他的编辑会话；这次是确认后才剥的。
