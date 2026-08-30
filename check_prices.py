#!/usr/bin/env python3
"""
AI 订阅价格监控脚本
==================
每天定时从各提供商拉取价格，与 data.json 中的记录对比，发现变动时：
  1. 写入报告文件（price_reports/）
  2. 弹出 macOS 系统通知
  3. 不会自动修改 data.json —— 由人工确认后手动更新

监控范围：
  - OpenRouter：公开 API，拿到精确美元价格
  - apifun：    公开 API 拿分组倍率，推算实际价格（价格 = 官方美元价 × 倍率）
  - V3 API：    公开 API 拿基础倍率（分组倍率不公开，用快照对比检测变动）

用法：
  python3 check_prices.py            # 正常检查
  python3 check_prices.py --verbose  # 打印详细过程

详细说明见同目录《价格监控指南.md》
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
CONFIG_FILE = BASE_DIR / "monitor_config.local.json"
SNAPSHOT_FILE = BASE_DIR / "price_state" / "v3_snapshot.json"
REPORT_DIR = BASE_DIR / "price_reports"

VERBOSE = "--verbose" in sys.argv

# ---------------------------------------------------------------------------
# 配置与常量
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "proxy": "",            # 如 "http://127.0.0.1:7897"，留空则不使用
    "fx_rate": 7.0,         # 美元兑人民币汇率
    "change_threshold_pct": 0.5,  # 价格变动超过该百分比才算变动（防舍入噪音）
}

# data.json 模型 → OpenRouter 模型 ID 映射
OPENROUTER_MODEL_MAP = {
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-fable-5": "anthropic/claude-fable-5",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "grok-4.6": "x-ai/grok-4.6",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash-0731",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro-0813",
    "gemini-3.7-flash": "google/gemini-3.7-flash",
}

# apifun 分组名 → 负责的模型（与用户当前使用的分组一致）
APIFUN_GROUP_MODELS = {
    "Claude Plus（精品）": ["claude-sonnet-5", "claude-opus-5", "claude-fable-5"],
    "Codex Pro（外接版）": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
    "Grok 企业版": ["grok-4.6"],
    "DeepSeek（自部署精选）": ["deepseek-v4-flash", "deepseek-v4-pro"],
}

# DeepSeek 官方人民币定价（空闲时段，apifun 倍率以此为基础计算）
DEEPSEEK_OFFICIAL_CNY = {
    "deepseek-v4-flash": {"input": 1.50, "output": 4.50, "cacheRead": 0.05},
    "deepseek-v4-pro": {"input": 4.50, "output": 13.50, "cacheRead": 0.15},
}

# V3 上追踪的模型名
V3_MODELS = [
    "claude-sonnet-5", "claude-opus-5", "claude-fable-5",
    "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
    "grok-4.6", "deepseek-v4-flash-0731", "deepseek-v4-pro-0813",
    "gemini-3.7-flash",
]


def log(msg):
    if VERBOSE:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"配置文件读取失败，使用默认配置: {e}")
    return cfg


def http_get_json(url, proxy=None, timeout=30, headers=None):
    """请求 JSON；先直连，失败且配置了代理则走代理重试。"""
    hdrs = {"User-Agent": "Mozilla/5.0 (ai-sub price monitor)"}
    if headers:
        hdrs.update(headers)

    def do_fetch(use_proxy):
        handler = None
        if use_proxy and proxy:
            handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=hdrs)
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return do_fetch(False)
    except Exception as e:
        if proxy:
            log(f"直连失败({e})，尝试代理 {proxy} ...")
            return do_fetch(True)
        raise


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

# 核验链接：发现变动后方便人工到源头确认
def verify_url(provider, model_id=None, or_id=None):
    if provider == "OpenRouter" and or_id:
        return f"https://openrouter.ai/{or_id}"
    if provider == "apifun":
        return "https://apikey.fun/pricing"
    if provider == "V3 API":
        return "https://api.v3.cm/panel"
    return ""


def check_openrouter(data, cfg):
    """对比 OpenRouter 实际价格与 data.json 记录。"""
    diffs = []
    log("拉取 OpenRouter 价格 ...")
    raw = http_get_json("https://openrouter.ai/api/v1/models", proxy=cfg.get("proxy"))
    api_prices = {m["id"]: m.get("pricing", {}) for m in raw.get("data", [])}
    fx = cfg["fx_rate"]

    or_entries = {p["model"]: p for p in data["prices"] if p["provider"] == "openrouter"}

    for model_id, or_id in OPENROUTER_MODEL_MAP.items():
        url = verify_url("OpenRouter", model_id, or_id)
        if or_id not in api_prices:
            diffs.append({"provider": "OpenRouter", "model": model_id, "field": "-",
                          "local": "-", "remote": "模型在 OpenRouter 上不存在", "url": url})
            continue
        p = api_prices[or_id]
        remote = {
            "input": float(p.get("prompt", 0)) * 1e6 * fx,
            "output": float(p.get("completion", 0)) * 1e6 * fx,
            "cacheRead": float(p.get("input_cache_read") or 0) * 1e6 * fx,
        }
        local = or_entries.get(model_id, {})
        for field, rv in remote.items():
            if rv == 0 and field == "cacheRead" and not local.get("cacheRead"):
                continue  # 双方都没有缓存价，跳过
            lv = local.get(field)
            if lv is None:
                if rv > 0:
                    diffs.append({"provider": "OpenRouter", "model": model_id, "field": field,
                                  "local": "(空)", "remote": f"¥{rv:.4f}", "url": url})
                continue
            if is_changed(lv, rv, cfg):
                diffs.append({"provider": "OpenRouter", "model": model_id, "field": field,
                              "local": f"¥{lv}", "remote": f"¥{rv:.4f}", "url": url})
    return diffs


# ---------------------------------------------------------------------------
# apifun
# ---------------------------------------------------------------------------

def check_apifun(data, cfg):
    """拉取 apifun 分组倍率，推算价格并与 data.json 对比。
    定价规则：CNY 价格 = 官方美元价 × 分组倍率（倍率已隐含汇率折算）
    DeepSeek 例外：官方价本身是人民币，直接 × 倍率。
    """
    diffs = []
    log("拉取 apifun 分组倍率 ...")
    raw = http_get_json(
        "https://apikey.fun/api/v1/pricing/groups?timezone=Asia%2FShanghai",
        proxy=cfg.get("proxy"))
    groups = {g["name"]: g for g in raw.get("data", [])}
    fx = cfg["fx_rate"]

    apifun_entries = {p["model"]: p for p in data["prices"] if p["provider"] == "apifun"}

    for group_name, model_ids in APIFUN_GROUP_MODELS.items():
        g = groups.get(group_name)
        if not g:
            diffs.append({"provider": "apifun", "model": "-", "field": "-",
                          "local": "-", "remote": f"分组「{group_name}」不存在（可能改名）",
                          "url": verify_url("apifun")})
            continue
        rate = g.get("rate_multiplier", 0)
        for model_id in model_ids:
            local = apifun_entries.get(model_id)
            if not local:
                continue
            if model_id in DEEPSEEK_OFFICIAL_CNY:
                base = DEEPSEEK_OFFICIAL_CNY[model_id]
                expected = {k: v * rate for k, v in base.items()}
            else:
                off = data["officialPrices"].get(model_id, {})
                expected = {
                    "input": off.get("input", 0) / fx * rate,
                    "output": off.get("output", 0) / fx * rate,
                    "cacheRead": (off.get("cacheRead") or 0) / fx * rate,
                }
                if off.get("cacheWrite"):
                    expected["cacheWrite"] = off["cacheWrite"] / fx * rate
            for field, ev in expected.items():
                lv = local.get(field)
                if ev == 0 or ev < 0.001:
                    continue
                if lv is None:
                    continue  # data.json 没记录该字段（如缓存写），不算变动
                if is_changed(lv, ev, cfg):
                    diffs.append({"provider": "apifun", "model": model_id, "field": field,
                                  "local": f"¥{lv}",
                                  "remote": f"¥{ev:.4f}（倍率 {rate}）",
                                  "url": verify_url("apifun")})
    return diffs


# ---------------------------------------------------------------------------
# V3 API（基础倍率，快照对比）
# ---------------------------------------------------------------------------

def check_v3(cfg):
    """拉取 V3 基础倍率，与上次快照对比。分组倍率不公开，无法算出最终价格，
    因此只检测倍率本身是否变动。"""
    diffs = []
    log("拉取 V3 API 基础倍率 ...")
    raw = http_get_json("https://api.v3.cm/api/pricing", proxy=cfg.get("proxy"))
    items = raw.get("data", raw if isinstance(raw, list) else [])
    remote = {}
    for it in items:
        name = it.get("model_name", "")
        if name in V3_MODELS:
            remote[name] = {
                "model_ratio": it.get("model_ratio"),
                "completion_ratio": it.get("completion_ratio"),
                "enable_groups": it.get("enable_groups", []),
            }

    snapshot = {}
    if SNAPSHOT_FILE.exists():
        with open(SNAPSHOT_FILE) as f:
            snapshot = json.load(f)

    if not snapshot:
        log("V3 无历史快照，本次作为基线保存。")
    else:
        for name, r in remote.items():
            s = snapshot.get(name)
            if s is None:
                diffs.append({"provider": "V3 API", "model": name, "field": "-",
                              "local": "(快照中无)", "remote": f"新增模型 倍率 {r['model_ratio']}/{r['completion_ratio']}",
                              "url": verify_url("V3 API")})
                continue
            for k in ("model_ratio", "completion_ratio"):
                if s.get(k) != r.get(k):
                    diffs.append({"provider": "V3 API", "model": name, "field": k,
                                  "local": f"{s.get(k)}", "remote": f"{r.get(k)}",
                                  "url": verify_url("V3 API")})
            if sorted(s.get("enable_groups", [])) != sorted(r.get("enable_groups", [])):
                diffs.append({"provider": "V3 API", "model": name, "field": "enable_groups",
                              "local": str(s.get("enable_groups")), "remote": str(r.get("enable_groups")),
                              "url": verify_url("V3 API")})

    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(remote, f, ensure_ascii=False, indent=2)
    return diffs


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def is_changed(local, remote, cfg):
    if local is None or remote is None:
        return local != remote
    if local == 0 and remote == 0:
        return False
    # 绝对差 ≤ ¥0.005 视为舍入误差（提供商页面通常只显示两位小数）
    if abs(local - remote) <= 0.005:
        return False
    base = max(abs(local), abs(remote), 1e-9)
    return abs(local - remote) / base * 100 > cfg["change_threshold_pct"]


def write_report(all_diffs, errors):
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    path = REPORT_DIR / f"report-{ts.strftime('%Y-%m-%d_%H%M')}.md"
    lines = [f"# 价格变动检查报告 {ts.strftime('%Y-%m-%d %H:%M')}", ""]
    if not all_diffs and not errors:
        lines.append("✅ 所有监控的提供商价格均无变动。")
    if all_diffs:
        lines.append(f"⚠️ 发现 {len(all_diffs)} 处变动（请确认后手动更新 data.json）：")
        lines.append("")
        lines.append("| 提供商 | 模型 | 字段 | 当前记录 | 线上最新 | 核验链接 |")
        lines.append("|--------|------|------|----------|----------|----------|")
        for d in all_diffs:
            link = f"[去核验]({d['url']})" if d.get("url") else "—"
            lines.append(f"| {d['provider']} | {d['model']} | {d['field']} | {d['local']} | {d['remote']} | {link} |")
    if errors:
        lines.append("")
        lines.append("## 检查失败的部分")
        for e in errors:
            lines.append(f"- {e}")
    lines.append("")
    lines.append("> 更新方法：编辑 `data.json` 中对应条目的价格，然后 `git add data.json && git commit && git push`")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def notify(n_diffs, n_errors, report_path):
    if n_diffs == 0 and n_errors == 0:
        log("无变动，不弹通知。")
        return
    if n_diffs > 0:
        title = f"AI 价格变动：发现 {n_diffs} 处差异"
        msg = f"报告：{report_path.name}（price_reports/ 目录）"
    else:
        title = "AI 价格检查：部分提供商查询失败"
        msg = f"{n_errors} 个检查项出错，详见报告"
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "{title}" sound name "Glass"'
        ], check=False, timeout=10)
    except Exception as e:
        log(f"通知发送失败: {e}")


def main():
    cfg = load_config()
    with open(DATA_FILE) as f:
        data = json.load(f)

    all_diffs, errors = [], []

    for name, fn in [
        ("OpenRouter", lambda: check_openrouter(data, cfg)),
        ("apifun", lambda: check_apifun(data, cfg)),
        ("V3 API", lambda: check_v3(cfg)),
    ]:
        try:
            diffs = fn()
            all_diffs.extend(diffs)
            log(f"{name}: {len(diffs)} 处变动")
        except Exception as e:
            errors.append(f"{name} 检查失败: {e}")
            log(f"{name} 检查失败: {e}")

    report_path = write_report(all_diffs, errors)
    print(f"检查完成：{len(all_diffs)} 处变动，{len(errors)} 个错误")
    print(f"报告：{report_path}")
    if all_diffs:
        for d in all_diffs:
            print(f"  [{d['provider']}] {d['model']} {d['field']}: {d['local']} → {d['remote']}")
    notify(len(all_diffs), len(errors), report_path)


if __name__ == "__main__":
    main()
