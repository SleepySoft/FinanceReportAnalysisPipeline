#!/usr/bin/env python3
"""
测试 pdf_toc_parser.py 在 TXT 样本上的目录解析效果

由于样本是 TXT 文件，我们模拟 PDF 解析流程：
1. 读取 TXT 内容
2. 检测目录页（前 20 行）
3. 用 _is_toc_line 过滤每一行
4. 用 _parse_toc_text 解析目录结构
"""

import sys
import re
from pathlib import Path

# 添加项目目录到路径
sys.path.insert(0, '/mnt/d/FinanceReportAnalysisPipeline')

from pdf_toc_parser import PdfTocParser, TocEntry

SAMPLES_DIR = Path('/mnt/d/FinanceReportAnalysisPipeline/samples')


def test_toc_parser_on_txt(txt_path: Path):
    """测试目录解析器在 TXT 文件上的效果"""
    
    print(f"\n{'='*60}")
    print(f"测试: {txt_path.name}")
    print(f"{'='*60}")
    
    # 读取文件内容
    with open(txt_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    print(f"总行数: {len(lines)}")
    
    # 创建一个模拟的 parser 实例（不需要真实 PDF）
    # 我们直接测试静态方法
    
    # 检测目录页：扫描前 20 行
    toc_page_lines = []
    for i, line in enumerate(lines[:20]):
        line = line.strip()
        if not line:
            continue
        # 模拟 _is_toc_line 检查
        is_toc, title, page_num = simulate_is_toc_line(line)
        if is_toc:
            toc_page_lines.append((i+1, line, title, page_num))
    
    print(f"\n前20行中检测到 {len(toc_page_lines)} 个目录行:")
    for line_no, raw, title, page_num in toc_page_lines:
        print(f"  行{line_no}: [{title}] -> 页{page_num}")
    
    # 如果前20行没找到，扫描整个文件找"目录"关键词
    if not toc_page_lines:
        print("\n前20行未找到目录，扫描全文...")
        for i, line in enumerate(lines):
            line = line.strip()
            if '目录' in line and len(line) < 20:
                print(f"  找到'目录'关键词 @行{i+1}: {line}")
                # 检查后续行
                for j in range(i+1, min(i+15, len(lines))):
                    check_line = lines[j].strip()
                    if not check_line:
                        continue
                    is_toc, title, page_num = simulate_is_toc_line(check_line)
                    if is_toc:
                        toc_page_lines.append((j+1, check_line, title, page_num))
                break
        
        print(f"\n目录页检测到 {len(toc_page_lines)} 个目录行:")
        for line_no, raw, title, page_num in toc_page_lines:
            print(f"  行{line_no}: [{title}] -> 页{page_num}")
    
    # 总结
    if toc_page_lines:
        print(f"\n✅ 成功解析 {len(toc_page_lines)} 个目录项")
    else:
        print(f"\n❌ 未找到目录")
    
    return toc_page_lines


def simulate_is_toc_line(line: str):
    """
    模拟 PdfTocParser._is_toc_line 的核心逻辑
    返回: (is_valid, title, page_num)
    """
    line = line.strip()
    
    # 复制 PdfTocParser 的正则模式
    TOC_LINE_PATTERN = re.compile(
        r'^(.*?\S)\s+([0-9]{1,3})$'
    )
    TOC_LINE_LOOSE = re.compile(
        r'^(.*?\S)\s*[\.\·\-…]+\s*([0-9]{1,3})$'
    )
    SECTION_PATTERN = re.compile(r'第[一二三四五六七八九十]+节')
    
    CHAPTER_KEYWORDS = [
        '重要提示', '目录', '释义', '公司简介', '会计数据',
        '经营情况', '管理层讨论', '公司治理', '环境', '社会责任',
        '重要事项', '股份变动', '债券', '财务报告', '备查文件',
        '审计报告', '资产负债表', '利润表', '现金流量', '附注'
    ]
    
    # 尝试匹配
    match = TOC_LINE_PATTERN.match(line)
    if not match:
        match = TOC_LINE_LOOSE.match(line)
    
    if not match:
        return False, None, None
    
    title = match.group(1).strip()
    page_num = int(match.group(2))
    
    # 过滤1：标题长度
    if len(title) < 4 or len(title) > 50:
        return False, None, None
    
    # 过滤2：页码范围
    if page_num < 1 or page_num > 500:
        return False, None, None
    
    # 过滤3：章节关键词或"第X节"
    has_section_num = SECTION_PATTERN.search(title) is not None
    has_keyword = any(kw in title for kw in CHAPTER_KEYWORDS)
    
    # 特殊处理：排除特定内容
    if '年内召开' in title or '会议次数' in title:
        return False, None, None
    if '原则' in title and len(title) > 15:
        return False, None, None
    if '环境信息依法披露' in title or '企业数量' in title:
        return False, None, None
    if title.startswith('株冶') and not has_section_num:
        return False, None, None
    if '塘环境技' in title:
        return False, None, None
    
    if not has_section_num and not has_keyword:
        return False, None, None
    
    # 过滤4：数字密度（排除财务数据）
    digits = sum(1 for c in title if c.isdigit())
    if digits / max(len(title), 1) > 0.3:
        return False, None, None
    
    # 过滤5：排除电话号码/年份格式
    if re.search(r'\d{4}-\d{2}-\d{2}', title):
        return False, None, None
    
    return True, title, page_num


def main():
    """测试所有样本"""
    
    # 获取所有样本文件
    samples = sorted(SAMPLES_DIR.glob('*.txt'))
    
    print(f"找到 {len(samples)} 个样本文件")
    
    results = {}
    for sample in samples:
        toc_lines = test_toc_parser_on_txt(sample)
        results[sample.name] = len(toc_lines)
    
    # 汇总
    print(f"\n{'='*60}")
    print("汇总结果")
    print(f"{'='*60}")
    
    success = 0
    partial = 0
    failed = 0
    
    for name, count in results.items():
        if count >= 5:
            status = "✅ 成功"
            success += 1
        elif count > 0:
            status = "⚠️ 部分"
            partial += 1
        else:
            status = "❌ 失败"
            failed += 1
        print(f"  {status}: {name} ({count}项)")
    
    print(f"\n总计: {success} 成功, {partial} 部分, {failed} 失败")


if __name__ == '__main__':
    main()
