#!/usr/bin/env python3
"""
financial_notes_splitter.py

财务报表附注关键章节提取器。

提取高价值章节（顺序保留，不摘抄）：
- 关联交易
- 或有事项（担保、诉讼、仲裁）
- 资产负债表日后事项
- 其他重要事项

Usage:
python financial_notes_splitter.py processed/2024_600519/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple


# ===== 附注高价值章节别名 =====
# 格式：标准编号 + 关键词
NOTES_KEY_SECTIONS = {
    "关联交易": [
        "十、关联方", "十、关联交易", "关联方及关联交易",
        "十一、关联方", "关联方关系",
    ],
    "或有事项": [
        "十二、或有事项", "十一、或有事项", "承诺及或有事项",
        "十三、或有事项", "预计负债",
    ],
    "资产负债表日后事项": [
        "十三、资产负债表日后事项", "十二、资产负债表日后事项",
        "十四、资产负债表日后事项", "期后事项",
    ],
    "其他重要事项": [
        "十四、其他重要事项", "十三、其他重要事项",
        "十五、其他重要事项", "其他重大事项",
        "十六、其他重要事项", "分部报告",
    ],
}


def find_key_sections(text: str) -> List[Tuple[str, str, int, int]]:
    """
    在附注中查找高价值章节位置。
    
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
        for canonical, alias_list in NOTES_KEY_SECTIONS.items():
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
    
    # 计算end位置（到下一个章节或文本结尾）
    result = []
    for i, (canonical, title, start) in enumerate(unique):
        if i + 1 < len(unique):
            end = unique[i + 1][2]
        else:
            end = len(text)
        result.append((canonical, title, start, end))
    
    return result


def split_notes(input_dir: Path) -> Dict:
    """拆分财务报表附注。"""
    notes_file = input_dir / "sections" / "financial_sub" / "财务报表附注.txt"
    
    # 如果附注没有独立拆分，尝试从合并所有者权益变动表文件找
    if not notes_file.exists():
        alt_file = input_dir / "sections" / "financial_sub" / "合并所有者权益变动表.txt"
        if alt_file.exists():
            notes_file = alt_file
        else:
            return {"error": "Notes file not found"}
    
    with open(notes_file, 'r', encoding='utf-8') as f:
        text = f.read()
    
    key_sections = find_key_sections(text)
    
    result = {
        "source": str(notes_file),
        "total_chars": len(text),
        "key_sections": [],
    }
    
    # 创建输出目录
    sub_dir = input_dir / "sections" / "notes_sub"
    sub_dir.mkdir(exist_ok=True)
    
    for canonical, title, start, end in key_sections:
        content = text[start:end].strip()
        if not content:
            continue
        
        # 保存章节
        safe_name = canonical.replace('/', '_')
        out_file = sub_dir / f"{safe_name}.txt"
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(content + '\n')
        
        result["key_sections"].append({
            "canonical_name": canonical,
            "raw_title": title,
            "char_count": len(content),
            "start": start,
            "end": end,
            "output_file": str(out_file.relative_to(input_dir)),
        })
    
    # 保存元数据
    meta_file = sub_dir / "_key_sections.json"
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract key sections from financial notes.")
    parser.add_argument("input_dir", help="processed directory containing sections/financial_sub/")
    
    args = parser.parse_args()
    
    input_dir = Path(args.input_dir)
    result = split_notes(input_dir)
    
    if "error" in result:
        print(f"Error: {result['error']}")
        return 1
    
    print(f"Notes: {len(result['key_sections'])} key sections extracted")
    for section in result["key_sections"]:
        print(f"  {section['canonical_name']}: {section['char_count']:,} chars")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
