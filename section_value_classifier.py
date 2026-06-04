#!/usr/bin/env python3
"""
section_value_classifier.py

A-share annual report section value classifier.

Goal:
- Input:  processed/ sections/*.txt + standard_sections.json
- Output: value_labels.json (metadata only, no file modification)
- No AI / rule-based classification

Value Tiers:
- strong: 强结构化高价值块
- semi: 半结构化高价值块
- weak: 弱价值但可索引
- low: 低价值/噪声块

Usage:
python section_value_classifier.py processed/2024_600519/
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ===== 价值分类规则 =====

VALUE_RULES = {
    "strong": {
        "sections": [
            "公司简介和主要财务指标",  # 包含财务指标表
            "财务报告",  # 包含财务报表
        ],
        "keywords": [
            "财务指标", "营业收入", "净利润", "总资产", "净资产",
            "资产负债表", "利润表", "现金流量表",
            "股东", "分红", "派息",
        ],
        "min_chars": 1000,
    },
    "semi": {
        "sections": [
            "管理层讨论与分析",
            "重要事项",
        ],
        "keywords": [
            "经营情况", "主营业务", "行业分析", "竞争格局",
            "风险因素", "重大事项", "诉讼", "仲裁", "担保",
            "环保", "处罚", "关联交易",
        ],
        "min_chars": 500,
    },
    "weak": {
        "sections": [
            "公司治理",
            "环境和社会责任",
            "股份变动及股东情况",
            "债券相关情况",
        ],
        "keywords": [
            "董事", "监事", "高管", "会议", "制度",
            "环保措施", "社会责任", "捐赠", "公益",
            "股份变动", "股东", "债券",
        ],
        "min_chars": 50,
    },
    "low": {
        "sections": [
            "重要提示_目录_释义",
        ],
        "keywords": [
            "重要提示", "目录", "释义", "备查文件",
            "免责声明", "审计意见",
        ],
        "min_chars": 0,
    },
}


def classify_section(
    section_name: str,
    content: str,
    char_count: int,
    confidence: float,
) -> Tuple[str, float, List[str]]:
    """
    对章节进行价值分类。
    
    Returns:
        (tier, score, evidence)
    """
    evidence = []
    
    # 1. 基于章节名的基础分类
    base_tier = None
    for tier, rules in VALUE_RULES.items():
        if section_name in rules["sections"]:
            base_tier = tier
            evidence.append(f"section_name:{tier}")
            break
    
    # 2. 基于关键词的强化
    content_lower = content.lower()
    keyword_scores = {}
    for tier, rules in VALUE_RULES.items():
        score = 0
        for kw in rules["keywords"]:
            if kw in content_lower:
                score += 1
        keyword_scores[tier] = score
    
    # 3. 基于长度的判断
    length_tier = None
    for tier in ["strong", "semi", "weak", "low"]:
        rules = VALUE_RULES[tier]
        if char_count >= rules["min_chars"]:
            length_tier = tier
            break
    
    # 4. 综合评分
    # 优先：章节名匹配 > 关键词匹配 > 长度
    if base_tier:
        final_tier = base_tier
    elif max(keyword_scores.values()) > 0:
        final_tier = max(keyword_scores, key=keyword_scores.get)
        evidence.append(f"keywords:{keyword_scores[final_tier]}")
    else:
        final_tier = length_tier or "low"
        evidence.append(f"length:{char_count}")
    
    # 5. 计算置信度分数
    score = confidence
    if char_count < 200:
        score *= 0.8  # 过短降权
    if keyword_scores.get(final_tier, 0) > 0:
        score = min(1.0, score + 0.1)  # 有关键词加分
    
    return final_tier, round(score, 2), evidence


def process_directory(input_dir: Path) -> Dict:
    """处理目录，生成价值标签。"""
    input_dir = Path(input_dir)
    
    # 读取标准章节信息
    std_file = input_dir / "standard_sections.json"
    if not std_file.exists():
        raise FileNotFoundError(f"No standard_sections.json found in {input_dir}")
    
    with open(std_file, "r", encoding="utf-8") as f:
        std_data = json.load(f)
    
    sections = std_data.get("sections", [])
    doc_id = std_data.get("doc_id", input_dir.name)
    
    # 分类结果
    labels = []
    tier_counts = {"strong": 0, "semi": 0, "weak": 0, "low": 0}
    
    for sec in sections:
        sec_name = sec.get("canonical_name", "")
        sec_file = input_dir / sec.get("output_file", "")
        
        # 读取内容
        content = ""
        if sec_file.exists():
            with open(sec_file, "r", encoding="utf-8") as f:
                content = f.read()
        
        char_count = sec.get("char_count", 0)
        confidence = sec.get("confidence", 0.0)
        
        # 分类
        tier, score, evidence = classify_section(
            sec_name, content, char_count, confidence
        )
        
        tier_counts[tier] += 1
        
        labels.append({
            "section_id": sec.get("section_id", ""),
            "canonical_name": sec_name,
            "tier": tier,
            "score": score,
            "char_count": char_count,
            "confidence": confidence,
            "evidence": evidence,
            "status": sec.get("status", "unknown"),
        })
    
    # 构建输出
    result = {
        "schema_version": "0.1.0",
        "doc_id": doc_id,
        "source_dir": str(input_dir),
        "classification": {
            "method": "rule_based",
            "tiers": {
                "strong": "强结构化高价值块",
                "semi": "半结构化高价值块",
                "weak": "弱价值但可索引",
                "low": "低价值/噪声块",
            },
        },
        "summary": {
            "total_sections": len(labels),
            "tier_counts": tier_counts,
            "strong_ratio": round(tier_counts["strong"] / len(labels) * 100, 1) if labels else 0,
        },
        "labels": labels,
    }
    
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify annual report sections by value tier.")
    parser.add_argument("input_dir", help="processed directory containing sections/")
    parser.add_argument("--out", default=None, help="output file (default: input_dir/value_labels.json)")
    
    args = parser.parse_args()
    
    input_dir = Path(args.input_dir)
    out_file = Path(args.out) if args.out else input_dir / "value_labels.json"
    
    result = process_directory(input_dir)
    
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Value labels saved to: {out_file}")
    print(f"Summary: {result['summary']}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
