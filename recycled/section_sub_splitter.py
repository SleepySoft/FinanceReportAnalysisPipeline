#!/usr/bin/env python3
"""
section_sub_splitter.py

Sub-section splitter for large annual report sections.

Splits:
1. 财务报告 → 审计报告 + 合并资产负债表 + 合并利润表 + 合并现金流量表 + 合并所有者权益变动表 + 财务报表附注
2. 管理层讨论与分析 → 主营业务 + 行业分析 + 经营情况 + 风险因素 + 未来战略

Usage:
python section_sub_splitter.py processed/2024_600519/
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ===== 财务报告子章节别名 =====
# 按标准顺序排列
FINANCIAL_SUB_ALIASES = {
    "审计报告": [
        "审计报告", "审计意见",
    ],
    "合并资产负债表": [
        "合并资产负债表",
    ],
    "合并利润表": [
        "合并利润表",
    ],
    "合并现金流量表": [
        "合并现金流量表",
    ],
    "合并所有者权益变动表": [
        "合并所有者权益变动表", "合并股东权益变动表",
    ],
    "财务报表附注": [
        "财务报表附注", "合并财务报表附注",
    ],
}

# ===== 管理层讨论与分析子章节别名 =====
MDA_SUB_ALIASES = {
    "主营业务": [
        "报告期内公司从事的主要业务", "公司的主营业务", "主要业务",
        "公司从事的主要业务", "主营业务情况",
    ],
    "行业分析": [
        "报告期内公司所处行业情况", "行业情况", "所处行业",
        "行业分析", "行业概况", "行业现状",
    ],
    "经营情况": [
        "经营情况讨论与分析", "主要经营情况", "经营情况分析",
        "报告期内经营情况", "经营成果",
    ],
    "风险因素": [
        "可能面对的风险", "公司面临的风险", "风险因素",
        "主要风险", "风险提示", "风险分析",
    ],
    "未来战略": [
        "公司未来发展的展望", "未来发展战略", "公司发展战略",
        "发展展望", "未来规划", "战略规划",
    ],
}


def find_sub_sections(text: str, aliases: Dict[str, List[str]]) -> List[Tuple[str, str, int, int]]:
    """
    在文本中查找子章节位置。
    
    Returns:
        [(canonical_name, matched_title, start_pos, end_pos), ...]
    """
    lines = text.split('\n')
    candidates = []
    
    for line_no, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        
        # 计算字符位置
        pos = sum(len(l) + 1 for l in lines[:line_no])
        
        # 匹配别名
        for canonical, alias_list in aliases.items():
            for alias in alias_list:
                if alias in line and len(line) < 50:
                    candidates.append((canonical, line, pos))
                    break
    
    # 去重：同一canonical保留第一个
    seen = set()
    unique = []
    for c in candidates:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)
    
    # 按位置排序
    unique.sort(key=lambda x: x[2])
    
    # 计算end位置
    result = []
    for i, (canonical, title, start) in enumerate(unique):
        if i + 1 < len(unique):
            end = unique[i + 1][2]
        else:
            end = len(text)
        result.append((canonical, title, start, end))
    
    return result


def split_financial_report(input_dir: Path) -> Dict:
    """拆分财务报告章节。"""
    fin_file = input_dir / "sections" / "09_财务报告.txt"
    if not fin_file.exists():
        return {"error": "Financial report not found"}
    
    with open(fin_file, 'r', encoding='utf-8') as f:
        text = f.read()
    
    # 策略：基于特征定位实际的表格位置
    # 1. 先找所有可能的标题位置
    raw_matches = find_sub_sections(text, FINANCIAL_SUB_ALIASES)
    
    # 2. 过滤：区分目录页标题和实际表格标题
    # 实际表格标题后面应该有"编制单位"或"项目"等表格特征
    sub_sections = []
    for canonical, title, start, end in raw_matches:
        if canonical == "审计报告":
            # 审计报告在开头
            sub_sections.append((canonical, title, start, end))
        else:
            # 检查后面是否有表格特征
            after = text[start:start+300]
            has_table = any(k in after for k in ["编制单位", "项目", "期末余额", "本期发生", "流动资产", "货币资金"])
            if has_table:
                sub_sections.append((canonical, title, start, end))
    
    # 3. 重新计算边界
    # 按位置排序
    sub_sections.sort(key=lambda x: x[2])
    
    # 更新end位置
    final_sections = []
    for i, (canonical, title, start, end) in enumerate(sub_sections):
        if i + 1 < len(sub_sections):
            end = sub_sections[i + 1][2]
        else:
            end = len(text)
        final_sections.append((canonical, title, start, end))
    
    # 4. 如果没有找到表格，尝试基于"编制单位"定位
    if len(final_sections) <= 1:
        # 备用策略：找所有"编制单位"位置
        units = []
        pos = 0
        while True:
            pos = text.find("编制单位", pos)
            if pos < 0:
                break
            units.append(pos)
            pos += 4
        
        # 基于"编制单位"位置推断表格
        # 每个表格通常有2-3个"编制单位"（每页一个），取第一个
        if len(units) >= 4:
            final_sections = [
                ("审计报告", "审计报告", 0, units[0]),
                ("合并资产负债表", "合并资产负债表", units[0], units[2]),
                ("合并利润表", "合并利润表", units[2], units[4]),
                ("合并现金流量表", "合并现金流量表", units[4], units[6]),
            ]
            if len(units) >= 8:
                final_sections.append(("合并所有者权益变动表", "合并所有者权益变动表", units[6], units[8]))
                # 附注从最后一个"编制单位"之后开始
                final_sections.append(("财务报表附注", "财务报表附注", units[8], len(text)))
            else:
                final_sections.append(("合并所有者权益变动表", "合并所有者权益变动表", units[6], len(text)))
    
    result = {
        "source": str(fin_file),
        "total_chars": len(text),
        "sub_sections": [],
    }
    
    # 创建子章节输出目录
    sub_dir = input_dir / "sections" / "financial_sub"
    sub_dir.mkdir(exist_ok=True)
    
    for canonical, title, start, end in final_sections:
        content = text[start:end].strip()
        if not content:
            continue
        
        # 保存子章节
        safe_name = canonical.replace('/', '_')
        out_file = sub_dir / f"{safe_name}.txt"
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(content + '\n')
        
        result["sub_sections"].append({
            "canonical_name": canonical,
            "raw_title": title,
            "char_count": len(content),
            "start": start,
            "end": end,
            "output_file": str(out_file.relative_to(input_dir)),
        })
    
    # 保存元数据
    meta_file = sub_dir / "_sub_sections.json"
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    return result


def split_management_analysis(input_dir: Path) -> Dict:
    """拆分管理层讨论与分析章节。"""
    mda_file = input_dir / "sections" / "03_管理层讨论与分析.txt"
    if not mda_file.exists():
        return {"error": "Management analysis not found"}
    
    with open(mda_file, 'r', encoding='utf-8') as f:
        text = f.read()
    
    sub_sections = find_sub_sections(text, MDA_SUB_ALIASES)
    
    result = {
        "source": str(mda_file),
        "total_chars": len(text),
        "sub_sections": [],
    }
    
    # 创建子章节输出目录
    sub_dir = input_dir / "sections" / "mda_sub"
    sub_dir.mkdir(exist_ok=True)
    
    for canonical, title, start, end in sub_sections:
        content = text[start:end].strip()
        if not content:
            continue
        
        # 保存子章节
        safe_name = canonical.replace('/', '_')
        out_file = sub_dir / f"{safe_name}.txt"
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(content + '\n')
        
        result["sub_sections"].append({
            "canonical_name": canonical,
            "raw_title": title,
            "char_count": len(content),
            "start": start,
            "end": end,
            "output_file": str(out_file.relative_to(input_dir)),
        })
    
    # 保存元数据
    meta_file = sub_dir / "_sub_sections.json"
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Split large sections into sub-sections.")
    parser.add_argument("input_dir", help="processed directory containing sections/")
    parser.add_argument("--sections", default="all", choices=["all", "financial", "mda"],
                        help="which sections to split")
    
    args = parser.parse_args()
    
    input_dir = Path(args.input_dir)
    
    if args.sections in ["all", "financial"]:
        result = split_financial_report(input_dir)
        print(f"Financial report: {len(result.get('sub_sections', []))} sub-sections")
        for sub in result.get("sub_sections", []):
            print(f"  {sub['canonical_name']}: {sub['char_count']:,} chars")
    
    if args.sections in ["all", "mda"]:
        result = split_management_analysis(input_dir)
        print(f"\nManagement analysis: {len(result.get('sub_sections', []))} sub-sections")
        for sub in result.get("sub_sections", []):
            print(f"  {sub['canonical_name']}: {sub['char_count']:,} chars")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
