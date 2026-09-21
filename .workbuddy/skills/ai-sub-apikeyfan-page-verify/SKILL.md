---
name: ai-sub-apikeyfan-page-verify
description: 核查 apikey.fan（apifun）价格时，绕过「分组倍率 API 拿不到最终价」的限制，直接执行定价页自身 JS 里的渲染函数，拿到页面真正展示的分组价/官方价/折后价。当用户说「apikey.fan/pricing 更新了」「apifun 某个分组改了」「核验 apifun 价格」时使用。同时覆盖分组名映射、页面过滤规则与已知陷阱。
agent_created: true
---

# apikey.fan 定价页核查（执行页面自己的渲染函数）

## 为什么需要这招

`check_prices.py` 的 `check_apifun()` 只拉**分组倍率**，再按公式
`价 = officialPrices / fx × 倍率`（DeepSeek/GLM 是 `officialPrices × 倍率`）反推。
这有两个盲区：

1. **页面有自己的「官方价表」和过滤/下限规则**，公式反推可能与页面展示不一致。
2. **分组倍率没有任何快照**（`price_state/` 只快照 V3）。若某分组在 `data.json` 里没有条目，
   倍率变化**完全静默**——只能靠「分组名不存在」这类副作用偶然发现。

所以人工核查时，直接跑页面自己的代码，拿**页面实际展示的数字**。

## 步骤

### 1. 拉分组倍率（公开接口）

```bash
curl -s https://apikey.fan/api/v1/pricing/groups   # 直连可用，无需代理
```

关注字段：`name` / `rate_multiplier` / `status` / `updated_at`。
`updated_at` 是判断「今天有没有改」的最快依据。

### 2. 找到当前定价页的 bundle

```bash
curl -s https://apikey.fan/pricing | grep -oE 'src="/assets/[^"]+"'
# → /assets/index-<hash>.js（入口，含 __vite__mapDeps 路由→chunk 映射）
```

在入口 JS 里搜 `__vite__mapDeps`，把 `"/pricing"` 路由那行的依赖索引对上文件名。
**真正含定价数据的是 `dashboardPricing-*.js`**（不是 `PricingView-*.js`，后者只负责渲染）。
hash 会随发版变化，别硬编码。

### 3. 把 chunk 改造成可在 node 里 eval 的模块

chunk 顶部是 `import{f as $}from"./index-*.js"`，尾部是 `export{x as y,...}`。三步替换：

```python
import re
js = open("dashboardPricing-XXXX.js").read()
# 1) 干掉 import，stub 掉 http 客户端
js = re.sub(r'^import\{[^}]*\}from"[^"]+";',
            'const $={get:async()=>{throw new Error("stub")}};', js, count=1)
# 2) export{a as b,c} → globalThis.__exp={b:a,c}
m = re.search(r'export\{(.*?)\};?\s*$', js, re.S)
pairs = []
for item in m.group(1).split(","):
    item = item.strip()
    src, alias = item.split(" as ") if " as " in item else (item, item)
    pairs.append(f"{alias}:{src}")
js = js[:m.start()] + "globalThis.__exp={" + ",".join(pairs) + "};"
open("dp_module.js","w").write(js)
```

> ⚠️ 直接 `export{...}` → `const __exp={...}` 会语法错误（`Unexpected identifier 'as'`），
> 必须按 `as` 拆成 `别名:原名`。

### 4. 调用页面自己的函数

导出的符号（`__exp` 的别名）语义：

| 别名 | 原名 | 作用 |
|------|------|------|
| `P` | `V` | 平台目录（含 `officialCny` / `channelBaseCny` / `periodPrices`） |
| `a` | `re` | **主渲染函数** `re(platformObj, groupObj, opts)` → 每行 `{model, period, prices:{input:{current,official},...}}` |
| `b` | `me` | 由 API 分组数据构造 `groupObj`：`me(platformId, apiGroups, {}, 'zh')` |
| `d` | `T` | 按分组过滤可见模型 |

```js
const exp = globalThis.__exp;
const groups = JSON.parse(fs.readFileSync("groups_api.json","utf8")).data;
const DS = exp.P.find(p => p.id === "deepseek");
for (const g of exp.b("deepseek", groups, {}, "zh")) {
  console.log("###", g.name, g.effectiveRate);
  for (const r of exp.a(DS, g, {locale:"zh"})) {
    const p = r.prices;
    console.log("  ", r.model.name, r.period || r.contextTier,
                p.input.current, p.output.current, p.cacheRead?.current);
  }
}
```

`platformId` 取值：`claude` / `codex` / `grok` / `gemini` / `zhipu` / `kimi` / `deepseek` / `minimax`
（注意是 `codex` 不是 `openai`，是 `zhipu` 不是 `glm`）。

### 5. 与 `data.json` 对比

`prices[]` 里 `provider == "apifun"` 的条目 vs 页面 `current`。
`current` 是页面展示价（保留 3 位小数），`official` 是官方价——**顺带就能核验 `officialPrices` 是否正确**。

## 已知陷阱

1. **`云厂商` 与 `大厂直供` 是同一渠道**
   页面判定 `D(g) = /云厂商|大厂直供/.test(g.name)`。
   2026-09-07 `自部署精选`→`云厂商渠道`，2026-09-21 `云厂商渠道`→`大厂直供`（倍率 0.75→0.5）。
   **分组改名不会改 `created_at`**，所以别用创建时间判断是否新建。

2. **DeepSeek 分组不止一个，别记错**
   同一时刻存在 `大厂直供`(0.5) 与 `官方直连`(0.9)。用户用的是前者（与旧「云厂商渠道」同类）。

3. **缓存读价有分组下限**
   大厂直供分组的 `deepseek-flash` 缓存读按 `Math.max(rate, 0.5)` 计——倍率低于 0.5 时下限生效。

4. **`T()` 的模型过滤规则**（解释了为什么某分组看不到某模型）
   - `claude`：非 `Max/官渠` 分组**不展示** `claude-fable-5` / `claude-fable-5-1`
   - 名称含 `企业` 才展示 `enterpriseOnly` 模型
   - `deepseek`：非官方且非大厂直供的分组**不展示** `deepseek-flash`；
     名称含 `官方直连` 的分组**不展示** `deepseek-v4-flash`

5. **官方价单位混用**：`officialCny`（人民币）与 `officialUsd`（美元）并存；
   `channelBaseCny` 是分组价基数。DeepSeek/GLM 只有人民币口径。

6. **`officialPrices` 交叉核验**：页面的 `officialCny` 可直接当官方价基准，
   与 OpenRouter `pricing.prompt × fx` 互为印证（2026-09-14 曾据此发现 `gemini-3.8-flash` 官方价录高 2 倍）。

## 收尾

- 倍率/分组变化 → 先报告再改（AGENTS.md 的「价格更新授权」）
- 改完同步：`check_prices.py` 的 `APIFUN_GROUP_MODELS` 分组名、`价格监控指南.md` 的分组倍率表
- 强烈建议补 `price_state/apifun_groups.json` 快照，否则无条目的分组倍率变化永远静默
