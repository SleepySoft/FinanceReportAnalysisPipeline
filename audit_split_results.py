#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报分割结果审计脚本

扫描 output/full_split/ 下的结构化输出，统计：
1. 各章节出现频率
2. 缺失核心章节的报告
3. 按缺失数量排序，重点调查缺失多的个体
4. 按缺失模式分组，便于发现新识别模式

用法：
    py audit_split_results.py
"""

import json
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple

# 标准章节集合（编号 -> 名称）
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
ALL_SECTION_NUMS = {num for num, _ in STANDARD_SECTIONS}

# 核心章节：这些如果缺失，说明分割可能有问题
CORE_SECTIONS = {"030", "040", "090"}

# 可选章节：这些本来就不是每份报告都有
OPTIONAL_SECTIONS = {"080", "100", "110", "120"}


def extract_numbered_name(filename: str) -> Tuple[str, str]:
    """从 '010_前言及重要提示.txt' 中提取 (编号, 名称)"""
    if not filename.endswith(".txt"):
        return ("", "")
    stem = filename[:-4]
    if "_" not in stem:
        return ("", "")
    number, name = stem.split("_", 1)
    return (number, name)


def audit_split_results(split_dir: Path, batch_report_path: Path) -> Dict:
    """审计分割结果"""

    # 1. 读取 batch_report 获取全局状态
    batch = json.loads(batch_report_path.read_text(encoding="utf-8"))

    # 只关注 success 状态的报告
    success_docs = [
        d for d in batch.get("details", []) if d["status"] == "success"
    ]
    total_success = len(success_docs)

    # 2. 扫描每个成功报告的目录
    section_counter = Counter()       # 每个章节出现的次数
    doc_sections = {}                 # doc_id -> {编号}
    doc_missing = {}                  # doc_id -> [缺失编号列表]
    missing_core = []                 # 缺失核心章节的报告
    section_sizes = defaultdict(list) # 每个章节的文件大小分布

    for doc in success_docs:
        doc_id = doc["doc_id"]
        doc_dir = split_dir / doc_id
        if not doc_dir.exists():
            continue

        # 收集该报告的所有章节
        found_numbers = set()
        for f in doc_dir.iterdir():
            if f.suffix != ".txt":
                continue
            number, name = extract_numbered_name(f.name)
            if not number:
                continue
            found_numbers.add(number)
            section_counter[number] += 1
            section_sizes[number].append(f.stat().st_size)

        doc_sections[doc_id] = found_numbers

        # 计算缺失的章节
        missing = sorted(ALL_SECTION_NUMS - found_numbers)
        doc_missing[doc_id] = missing

        # 检查是否缺失核心章节
        missing_core_here = CORE_SECTIONS - found_numbers
        if missing_core_here:
            missing_core.append({
                "doc_id": doc_id,
                "missing": sorted(missing_core_here),
                "missing_all": missing,
                "has": sorted(found_numbers),
                "has_count": len(found_numbers),
                "missing_count": len(missing),
            })

    # 3. 按缺失数量排序（缺失越多越靠前）
    docs_by_missing = sorted(
        [(doc_id, missing) for doc_id, missing in doc_missing.items()],
        key=lambda x: (-len(x[1]), x[0])
    )

    # 4. 按缺失模式分组
    missing_pattern_counter = Counter()
    for doc_id, missing in docs_by_missing:
        if missing:
            pattern = "+".join(missing)
            missing_pattern_counter[pattern] += 1

    # 5. 章节大小统计
    section_size_stats = {}
    for number, sizes in section_sizes.items():
        if sizes:
            section_size_stats[number] = {
                "count": len(sizes),
                "min": min(sizes),
                "max": max(sizes),
                "median": sorted(sizes)[len(sizes) // 2],
                "mean": int(sum(sizes) / len(sizes)),
                "tiny_count": sum(1 for s in sizes if s < 200),
            }

    # 6. 章节数量分布
    section_count_dist = Counter(len(s) for s in doc_sections.values())

    return {
        "total_success": total_success,
        "section_counter": dict(section_counter),
        "section_count_distribution": dict(section_count_dist),
        "missing_core": missing_core,
        "missing_core_count": len(missing_core),
        "docs_by_missing": docs_by_missing,
        "missing_pattern_counter": dict(missing_pattern_counter),
        "section_size_stats": section_size_stats,
    }


def print_report(report: Dict):
    """打印审计报告"""

    total = report["total_success"]

    print("=" * 70)
    print("财报分割结果审计报告")
    print("=" * 70)
    print("成功分割的报告总数: %d" % total)
    print()

    # 1. 章节数量分布
    print("-" * 70)
    print("【一】每份报告识别到的章节数量分布")
    print("-" * 70)
    for count in sorted(report["section_count_distribution"].keys()):
        n = report["section_count_distribution"][count]
        pct = n / total * 100
        bar = "#" * int(pct / 2)
        print("  %2d 个章节: %5d 份 (%5.1f%%) %s" % (count, n, pct, bar))
    print()

    # 2. 各章节出现频率
    print("-" * 70)
    print("【二】各章节出现频率")
    print("-" * 70)
    print("  编号 章节名                    出现次数    占比    缺失数   状态")
    print("  " + "-" * 62)
    for num, name in STANDARD_SECTIONS:
        count = report["section_counter"].get(num, 0)
        pct = count / total * 100 if total > 0 else 0
        missing = total - count
        if count < total * 0.5:
            status = "[WARN-L]"
        elif count < total * 0.9:
            status = "[WARN-M]"
        else:
            status = "[OK]    "
        print("  %s %-22s %8d %6.1f%% %8d  %s" % (num, name, count, pct, missing, status))
    print()

    # 3. 章节大小异常
    print("-" * 70)
    print("【三】章节大小异常（极小章节 < 200 字节）")
    print("-" * 70)
    has_tiny = False
    for num, name in STANDARD_SECTIONS:
        stats = report["section_size_stats"].get(num)
        if stats and stats["tiny_count"] > 0:
            has_tiny = True
            print("  %s %s: %d 个文件极小" % (num, name, stats["tiny_count"]))
    if not has_tiny:
        print("  未发现极小章节")
    print()

    # 4. Top 10 缺失最多的报告
    print("-" * 70)
    print("【四】Top 10 缺失章节最多的报告（重点调查）")
    print("-" * 70)
    top_missing = [(doc_id, missing) for doc_id, missing in report["docs_by_missing"] if missing][:10]
    if top_missing:
        for rank, (doc_id, missing) in enumerate(top_missing, 1):
            missing_names = ["%s(%s)" % (SECTION_NAMES.get(m, m), m) for m in missing]
            print("  #%d %s" % (rank, doc_id))
            print("     缺失 %d 个: %s" % (len(missing), ", ".join(missing_names)))
    else:
        print("  所有报告都包含全部章节")
    print()

    # 5. 缺失核心章节的报告
    print("-" * 70)
    print("【五】缺失核心章节的报告（核心: %s）" % ", ".join(CORE_SECTIONS))
    print("-" * 70)
    print("  缺失核心章节的报告数: %d" % report["missing_core_count"])
    print()

    if report["missing_core"]:
        # 按缺失数量排序
        sorted_core = sorted(report["missing_core"], key=lambda x: (-x["missing_count"], x["doc_id"]))

        print("  按缺失模式分组:")
        pattern_counter = Counter()
        for item in sorted_core:
            pattern = "+".join(item["missing"])
            pattern_counter[pattern] += 1
        for pattern, count in pattern_counter.most_common():
            names = [SECTION_NAMES.get(p, p) for p in pattern.split("+")]
            print("    缺失 [%s]: %d 份  (%s)" % (pattern, count, ", ".join(names)))
        print()

        print("  详细列表（按缺失数量排序，前 20 条）:")
        for item in sorted_core[:20]:
            missing_names = ["%s(%s)" % (SECTION_NAMES.get(m, m), m) for m in item["missing"]]
            has_names = ["%s(%s)" % (SECTION_NAMES.get(m, m), m) for m in item["has"]]
            print("    %s" % item["doc_id"])
            print("      缺失 %d 个: %s" % (len(item["missing"]), ", ".join(missing_names)))
            print("      拥有 %d 个: %s" % (len(item["has"]), ", ".join(has_names)))
    else:
        print("  所有成功报告都包含全部核心章节 [OK]")
    print()

    # 6. 可选章节缺失情况
    print("-" * 70)
    print("【六】可选章节缺失情况（正常，供参考）")
    print("-" * 70)
    for num, name in STANDARD_SECTIONS:
        if num in OPTIONAL_SECTIONS:
            count = report["section_counter"].get(num, 0)
            missing = total - count
            if missing > 0:
                print("  %s %s: %d 份缺失" % (num, name, missing))
    print()

    print("=" * 70)


def main():
    split_dir = Path("output/full_split")
    batch_report_path = split_dir / "_batch_report.json"

    if not batch_report_path.exists():
        print("错误: 找不到 %s" % batch_report_path)
        print("请先运行 txt_report_splitter.py 生成分割结果")
        return

    report = audit_split_results(split_dir, batch_report_path)
    print_report(report)

    # 同时保存为 JSON 便于后续分析
    json_path = split_dir / "_audit_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("审计报告已保存: %s" % json_path)


if __name__ == "__main__":
    main()
