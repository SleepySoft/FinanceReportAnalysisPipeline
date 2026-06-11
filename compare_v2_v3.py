#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2 vs v3 财报分割结果对比脚本"""

import json
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, Set

V2_DIR = Path("output/full_split_v2")
V3_DIR = Path("output/full_split_v3")

STANDARD_SECTIONS = [
    ("010", "前言及重要提示"),
    ("020", "重要提示、目录和释义"),
    ("030", "公司简介和主要财务指标"),
    ("040", "管理层讨论与分析"),
    ("050", "公司治理、环境和社会"),
    ("060", "重要事项"),
    ("070", "股份变动及股东情况"),
    ("080", "债券相关情况"),
    ("090", "财务报告"),
    ("100", "审计报告"),
    ("110", "关联方及关联交易"),
    ("120", "重要交易和事项"),
]
SECTION_NAMES = {num: name for num, name in STANDARD_SECTIONS}
CORE_SECTIONS = {"030", "040", "050", "060", "070", "080", "090", "100"}


def load_batch_report(path: Path) -> Dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_doc_sections(doc_dir: Path) -> Set[str]:
    if not doc_dir.exists():
        return set()
    sections = set()
    for f in doc_dir.iterdir():
        if f.suffix == ".txt" and "_" in f.stem:
            number = f.stem.split("_", 1)[0]
            if number.isdigit():
                sections.add(number)
    return sections


def get_doc_section_sizes(doc_dir: Path) -> Dict[str, int]:
    if not doc_dir.exists():
        return {}
    sizes = {}
    for f in doc_dir.iterdir():
        if f.suffix == ".txt" and "_" in f.stem:
            number = f.stem.split("_", 1)[0]
            if number.isdigit():
                sizes[number] = f.stat().st_size
    return sizes


def compare():
    print("=" * 70)
    print("v2 vs v3 财报分割结果对比报告")
    print("=" * 70)

    v2_batch = load_batch_report(V2_DIR / "_batch_report.json")
    v3_batch = load_batch_report(V3_DIR / "_batch_report.json")

    if not v2_batch or not v3_batch:
        print("错误: 缺少 batch report，请确保两个版本都已跑完")
        return

    print("\n【一】全局处理指标对比")
    print("-" * 70)
    for key in ["total_files", "success", "warning", "failed", "ignored_summary", "ignored_low_quality", "elapsed_seconds"]:
        v2_val = v2_batch.get(key, 0)
        v3_val = v3_batch.get(key, 0)
        delta = v3_val - v2_val
        delta_str = f"(+{delta})" if delta > 0 else f"({delta})" if delta < 0 else "(=)"
        print(f"  {key:20s}: v2={v2_val:6}  v3={v3_val:6}  {delta_str}")

    v2_details = {d["doc_id"]: d for d in v2_batch.get("details", [])}
    v3_details = {d["doc_id"]: d for d in v3_batch.get("details", [])}
    all_doc_ids = sorted(set(v2_details.keys()) | set(v3_details.keys()))

    print(f"\n【二】各章节覆盖率对比")
    print("-" * 70)
    print(f"  {'编号':<5} {'章节名':<22} {'v2':>8} {'v3':>8} {'Δ':>8} {'趋势':<6}")
    print("  " + "-" * 60)

    v2_section_counts = Counter()
    v3_section_counts = Counter()

    for doc_id in v2_details:
        if v2_details[doc_id]["status"] == "success":
            secs = get_doc_sections(V2_DIR / doc_id)
            for s in secs:
                v2_section_counts[s] += 1

    for doc_id in v3_details:
        if v3_details[doc_id]["status"] == "success":
            secs = get_doc_sections(V3_DIR / doc_id)
            for s in secs:
                v3_section_counts[s] += 1

    v2_total_success = sum(1 for d in v2_details.values() if d["status"] == "success")
    v3_total_success = sum(1 for d in v3_details.values() if d["status"] == "success")

    for num, name in STANDARD_SECTIONS:
        v2_cnt = v2_section_counts.get(num, 0)
        v3_cnt = v3_section_counts.get(num, 0)
        v2_pct = v2_cnt / v2_total_success * 100 if v2_total_success > 0 else 0
        v3_pct = v3_cnt / v3_total_success * 100 if v3_total_success > 0 else 0
        delta = v3_cnt - v2_cnt
        delta_pct = v3_pct - v2_pct
        trend = "↑" if delta > 0 else "↓" if delta < 0 else "="
        print(f"  {num}  {name:<20} {v2_cnt:>6} {v3_cnt:>6} {delta:+6}  {trend} {delta_pct:+.2f}%")

    print(f"\n【三】逐报告差异分析（核心章节）")
    print("-" * 70)

    gained = defaultdict(list)
    lost = defaultdict(list)
    size_changed = []

    for doc_id in all_doc_ids:
        v2_status = v2_details.get(doc_id, {}).get("status", "missing")
        v3_status = v3_details.get(doc_id, {}).get("status", "missing")
        if v2_status != "success" or v3_status != "success":
            continue

        v2_secs = get_doc_sections(V2_DIR / doc_id)
        v3_secs = get_doc_sections(V3_DIR / doc_id)

        for num in CORE_SECTIONS:
            if num in v3_secs and num not in v2_secs:
                gained[num].append(doc_id)
            elif num in v2_secs and num not in v3_secs:
                lost[num].append(doc_id)

        v2_sizes = get_doc_section_sizes(V2_DIR / doc_id)
        v3_sizes = get_doc_section_sizes(V3_DIR / doc_id)
        for num in (v2_secs & v3_secs):
            v2_s = v2_sizes.get(num, 0)
            v3_s = v3_sizes.get(num, 0)
            if abs(v3_s - v2_s) > 1000:
                size_changed.append((doc_id, num, v2_s, v3_s))

    print(f"\n  v3 新增（v2 没有 → v3 有）：")
    for num in CORE_SECTIONS:
        name = SECTION_NAMES.get(num, num)
        print(f"    {name}({num}): {len(gained[num])} 份")
        for doc_id in gained[num][:5]:
            print(f"      - {doc_id}")
        if len(gained[num]) > 5:
            print(f"      ... 共 {len(gained[num])} 份")

    print(f"\n  v3 丢失（v2 有 → v3 没有）：")
    for num in CORE_SECTIONS:
        name = SECTION_NAMES.get(num, num)
        print(f"    {name}({num}): {len(lost[num])} 份")
        for doc_id in lost[num][:5]:
            print(f"      - {doc_id}")
        if len(lost[num]) > 5:
            print(f"      ... 共 {len(lost[num])} 份")

    if size_changed:
        print(f"\n【四】章节大小显著变化（|Δ| > 1KB）")
        print("-" * 70)
        size_changed.sort(key=lambda x: abs(x[3] - x[2]), reverse=True)
        print(f"  {'报告ID':<25} {'章节':<20} {'v2大小':>10} {'v3大小':>10} {'变化':>10}")
        for doc_id, num, v2_s, v3_s in size_changed[:20]:
            delta = v3_s - v2_s
            print(f"  {doc_id:<25} {SECTION_NAMES.get(num, num):<18} {v2_s:>10,} {v3_s:>10,} {delta:+10,}")

    result = {
        "v2_total_success": v2_total_success,
        "v3_total_success": v3_total_success,
        "section_counts": {
            num: {"v2": v2_section_counts.get(num, 0), "v3": v3_section_counts.get(num, 0)}
            for num, _ in STANDARD_SECTIONS
        },
        "core_changes": {
            "gained": {num: gained[num] for num in CORE_SECTIONS},
            "lost": {num: lost[num] for num in CORE_SECTIONS},
        },
        "size_changes_top20": [
            {"doc_id": d, "section": SECTION_NAMES.get(s, s), "v2_size": v1, "v3_size": v2, "delta": v2 - v1}
            for d, s, v1, v2 in size_changed[:20]
        ],
    }

    out_path = Path("output/v2_v3_comparison.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细对比数据已保存: {out_path}")
    print("=" * 70)


if __name__ == "__main__":
    compare()
