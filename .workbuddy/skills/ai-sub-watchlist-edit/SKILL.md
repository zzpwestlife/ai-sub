---
name: ai-sub-watchlist-edit
description: 在 ai-sub 项目里增删「观察模型」的完整工作流。当用户说「某模型发布了，加入观察列表」「某模型全部删掉」「加上某模型」时使用。覆盖 data.json、check_prices.py 八个硬编码名单、check_leaderboard.py 榜单映射、订阅/场景引用迁移、峰谷（空闲/高峰）定价字段位、以及文档同步。
agent_created: true
---

# ai-sub 观察模型增删工作流

「观察模型」在项目里有两个载体，别搞混：

| 载体 | 位置 | 作用 |
|------|------|------|
| **价格观察名单** | `data.json` → `models[]` | 主战场。一条模型 = 一组引用（见下） |
| **榜单观察名单** | `check_leaderboard.py` → `MODEL_SLUG_MAP` | aihot 榜单排名追踪 |

## 一个模型的完整引用面（改前必查）

```
data.json:
  models[]            模型定义（id/name/category/desc）
  officialPrices[id]  官方价（人民币；Claude/OpenAI 系是 美元×7）
  prices[]            (model, provider) 渠道报价
  openrouterRefMin[id] 最低参考价（脚本自动维护，可预填）
  subscriptions[].models  各订阅的「覆盖模型」列表  ← 最容易漏
  scenarios[].model / .backup  推荐组合的主模型与备用  ← 最容易漏
check_prices.py:      约 8 处常量（见下）
check_leaderboard.py: MODEL_SLUG_MAP
leadeboard_data.json: 历史快照，【不要动】
price_reports/ price_history.json: 历史，【不要动】
```

**先跑一遍引用面统计再动手**，别凭印象。

## 关键陷阱（血泪）

1. **各渠道函数的「先查本地记录」顺序不一致**
   - `check_aihubmix` / `check_nonelinear` / `check_apifun`：先 `local = entries.get(id); if not local: continue` → 本地无记录则**静默跳过**，名单里留旧名无害。
   - `check_openrouter`：`if or_id not in existing:` 判断在查本地记录**之前** → **给尚未上线的渠道加映射会每天报「模型不存在」**。加映射前必须先验证渠道真的有该 ID。
   - `check_v3` 是纯快照机制，只写 `price_state/v3_snapshot.json`，不读 `data.json`。

2. **渠道命名不统一，需要映射**
   - OpenRouter：`anthropic/claude-fable-5.1`（点号）
   - AIHubMix / V3：`claude-fable-5-1`（连字符）→ 靠 `AIHUBMIX_ID_MAP`
   - DeepSeek 官方 API 名：`deepseek-flash`（不带版本号）；聚合器用 `deepseek-v4.1-flash`

3. **aihubmix 的 `model_ratio` 会随时调整**：录入前必须实算，**绝不能照抄旧模型记录**。
   ```
   input(¥)  = 2 × model_ratio × (1 - promotion.off_percent/100) × fx_rate(7)
   output(¥) = input × completion_ratio
   cacheRead = input × cache_ratio
   ```

4. **删除模型时 `glm-5.3` 会假阳性**：`glm-5.3-flash` 是它的子串。校验要用负向断言 `glm-5\.3(?![\w.\-])`，别用 `count()`。

5. **峰谷定价模型的官方价有两个字段位，写错会连带弄坏 apifun 折扣**
   DeepSeek 系官方价分「空闲 / 高峰」两档，`officialPrices` 里：
   ```
   input / output / cacheRead          ← 空闲价（峰谷模型必须放这里）
   peakInput / peakOutput / peakCacheRead  ← 高峰价
   ```
   - **空闲价必须留在 `input/output`**：apifun 的人民币折扣按「官方空闲价 × 分组倍率」算
     （见 `check_prices.py` 的 `CNY_OFFICIAL_MODELS`），搬走就会算错。
   - 看板统一按**高峰档**展示与 `vs 官方` 对比（`index.html` 的 `officialFor()`，
     自动 fallback：有 `peakInput` 取峰值，否则取 `input`），并打 `<峰值>` 标记 +
     在备注里给出空闲档价。**加峰谷模型不用改看板**，只要字段位对。
   - `prices[]` 里官方要**两条**记录：空闲的（`notes: "空闲时段"`）和高峰的（`isPeak: true`）。
     看板会把 `isPeak` 的那条单独渲染成暗色「⚠️高峰」行，并排除在最优/排序计算之外。
   - 第三方的报价照常只写一条（第三方通常不区分峰谷）。

6. **别只看调用点就断言"数值不一致"**：`compositePrice(price, official)` 和
   `effectiveInput(price, official)` 的第二个参数**声明了但从未使用**（死参数）。
   传空闲价还是峰值价进去结果完全相同。**要先读被调函数体再下结论**，否则会报一个不存在的 bug。

## 步骤

1. **核查来源**（改价格前必须）
   - 官方价：`https://api-docs.deepseek.com/zh-cn/quick_start/pricing` 等官方定价页
   - 渠道价：OpenRouter `/api/v1/models` + `/models/{id}/endpoints`、aihubmix `mdl_info_pagination`、nonelinear `/models`、v3 `api/v3.cm/api/pricing`（**v3 与 aihubmix 必须走代理** `127.0.0.1:7897`）
   - 有多源时交叉验证（例：Fable 5.1 的 cacheRead 2.5% 由 OpenRouter + AIHubMix 双源确认）

2. **改 `data.json`**（用 Python 脚本，保 key 顺序 + `indent=2, ensure_ascii=False` + 末尾换行）
   - 新模型插到同系列锚点之后（保持分组可读）
   - 订阅/场景引用：有承接模型就**迁移**，无承接就**移除**（避免悬空 id）
   - `updateDate` 不手改（脚本维护）

3. **改 `check_prices.py`** 八个常量：
   `OPENROUTER_MODEL_MAP`、`APIFUN_GROUP_MODELS`（含分组名与倍率）、`CNY_OFFICIAL_MODELS`、`NONELINEAR_MODELS`、`AIHUBMIX_MODELS`、`AIHUBMIX_ID_MAP`、`V3_MODELS`、`CUBENCE_MODELS`
   - 归属分组可能比模型 id 更宽，别把整组删掉
   - V3 用平台侧名（可能带日期后缀）

4. **改 `check_leaderboard.py` → `MODEL_SLUG_MAP`**：点号转连字符；先确认 aihot 榜单真的收录（抓 `/leaderboard` 提取 `/leaderboard/{slug}`）

5. **干跑验证（无副作用，务必做）**
   ```python
   import importlib.util, json
   spec = importlib.util.spec_from_file_location("cp", "check_prices.py")
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   data = json.load(open("data.json", encoding="utf-8")); cfg = m.load_config()
   for fn, n in [(m.check_openrouter,"or"),(m.check_apifun,"af"),
                 (m.check_nonelinear,"nl"),(m.check_aihubmix,"hm")]:
       print(n, fn(data, cfg))
   ```
   期望：`diffs` 里**不应出现 `field == "-"`**（那是「模型不存在」类误报）。
   **不要跑 `check_prices.py` 全量**——它会写快照/历史/报告并可能弹通知。

6. **同步文档**：`价格监控指南.md`（OpenRouter 映射表、apifun 分组表、slug 说明）、`模型宣称与价格核查手册.md`（案例结论）。
   历史调价记录（`price_reports/`、指南里的旧案例）**保留不改**。

## 收尾

- 报告「未上线渠道」清单，等渠道跟进后补映射
- 干跑发现的**其它模型**价格变动（非本次改动引入）单独报告，不要顺手改
