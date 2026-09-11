"""非线智能峰谷容差的回归测试。

用法: python3 test_nonelinear_peakguard.py <check_prices.py 路径>
对照运行: 对 HEAD 版本应 FAIL（漏报），对修复后版本应 PASS。
"""
import importlib.util
import json
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("cp", path)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

# 被追踪的两个模型：一个无峰谷定价(GLM)，一个有峰谷定价(DeepSeek)
cp.NONELINEAR_MODELS = ["glm-5.3-flash", "deepseek-v4.1-flash"]

# 线上返回值：GLM 恰好是本地记录的 2 倍（促销结束），DeepSeek 是本地空闲价的 2 倍（峰值档）
ROWS = {"data": [
    ["glm-5.3-flash", "智谱AI", "Yes", 0.23, "0.80", "2.80", "/"],
    ["deepseek-v4.1-flash", "DeepSeek", "Yes", 0.04, "2.00", "8.00", "/"],
]}
cp.http_get_json = lambda url, proxy=None: ROWS

cfg = dict(cp.DEFAULT_CONFIG)
cfg["proxy"] = ""


def run(label, prices, official, expect):
    data = {"prices": prices, "officialPrices": official}
    try:
        diffs = cp.check_nonelinear(data, cfg, use_proxy=False)
    except TypeError:
        diffs = cp.check_nonelinear(data, cfg)
    fields = sorted((d["model"], d["field"]) for d in diffs)
    ok = len(diffs) == expect
    print(f"{'PASS' if ok else 'FAIL'}  {label}: 期望 {expect} 条, 实得 {len(diffs)} 条 {fields}")
    return ok


results = []

# 用例 1：GLM 无峰谷定价，线上恰为 2 倍 → 必须报出来（这正是本次被漏掉的真变动）
results.append(run(
    "GLM 促销结束(2倍,无峰谷定价)→应报",
    [{"model": "glm-5.3-flash", "provider": "nonelinear",
      "input": 0.4, "output": 1.4, "cacheRead": 0.115}],
    {"glm-5.3-flash": {"input": 0.8, "output": 2.8, "cacheRead": 0.23}},
    3,
))

# 用例 2：DeepSeek 有峰谷定价，线上为峰值档(2倍) → 仍应跳过，不报（保留原设计意图）
results.append(run(
    "DeepSeek 峰值档(2倍,有峰谷定价)→应跳过",
    [{"model": "deepseek-v4.1-flash", "provider": "nonelinear",
      "input": 1.0, "output": 4.0, "cacheRead": 0.02}],
    {"deepseek-v4.1-flash": {"input": 1.0, "output": 4.0, "cacheRead": 0.02,
                             "peakInput": 2.0, "peakOutput": 8.0}},
    0,
))

# 用例 3：完全同价 → 无变动
results.append(run(
    "同价→无变动",
    [{"model": "glm-5.3-flash", "provider": "nonelinear",
      "input": 0.8, "output": 2.8, "cacheRead": 0.23}],
    {"glm-5.3-flash": {"input": 0.8, "output": 2.8, "cacheRead": 0.23}},
    0,
))

print()
print("结果:", "全部通过" if all(results) else "存在失败用例")
sys.exit(0 if all(results) else 1)
