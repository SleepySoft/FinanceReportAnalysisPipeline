#!/usr/bin/env python3
"""
PDF原生结构目录识别与切片系统
直接从PDF页面提取文字结构，绕过TXT转换
支持多种目录格式：
  1. 三栏布局（大字体页码 + 小字体标题）
  2. 无页码目录（章节号 + 标题 + 点号线）
  3. 标准目录（标题 + 点号线 + 页码）
"""

import fitz  # PyMuPDF
import re
import json
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from collections import defaultdict


@dataclass
class TextBlock:
    """PDF页面中的文字块，带坐标和样式信息"""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font: str
    size: float
    flags: int
    page: int
    
    @property
    def is_bold(self) -> bool:
        # flags bit 4 = bold
        return bool(self.flags & 16)
    
    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2
    
    @property
    def width(self) -> float:
        return self.x1 - self.x0


@dataclass
class TOCItem:
    """目录条目"""
    title: str
    page: int
    level: int
    raw_text: str = ""


@dataclass
class Chapter:
    """识别的章节"""
    title: str
    start_page: int
    end_page: int
    level: int
    toc_item: TOCItem
    verified: bool = False


class PDFStructureExtractor:
    """PDF结构提取器"""
    
    def __init__(self, pdf_path: str):
        self.doc = fitz.open(pdf_path)
        self.page_width = self.doc[0].rect.width if self.doc.page_count > 0 else 612
        self.page_height = self.doc[0].rect.height if self.doc.page_count > 0 else 792
        
    def __del__(self):
        if hasattr(self, 'doc'):
            self.doc.close()
    
    def extract_text_blocks(self, page_num: int) -> List[TextBlock]:
        """提取页面的文字块，带坐标和样式信息"""
        page = self.doc[page_num]
        blocks = []
        
        # 使用 dict 模式获取详细结构
        text_dict = page.get_text("dict")
        
        for block in text_dict["blocks"]:
            if "lines" not in block:
                continue
                
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text or len(text) < 2:
                        continue
                        
                    bbox = span["bbox"]
                    blocks.append(TextBlock(
                        text=text,
                        x0=bbox[0],
                        y0=bbox[1],
                        x1=bbox[2],
                        y1=bbox[3],
                        font=span["font"],
                        size=span["size"],
                        flags=span["flags"],
                        page=page_num
                    ))
        
        # 按y坐标排序（从上到下）
        blocks.sort(key=lambda b: b.y0)
        return blocks
    
    def _is_footer_text(self, text: str) -> bool:
        """判断是否为页眉页脚文本"""
        footer_patterns = [
            r'^第.*页$',
            r'^Page\s*\d+$',
            r'^©',
            r'^版权所有',
            r'^CONTENTS$',
            r'^目\s*录$',
            r'^备查文件',
            r'^载有公司',
            r'^载有会计',
            r'^报告期内',
        ]
        for p in footer_patterns:
            if re.search(p, text, re.I):
                return True
        return False
    
    def find_toc_pages(self, max_pages: int = 50) -> List[Tuple[int, float]]:
        """
        查找目录页
        返回: [(页码, 置信度), ...]
        """
        candidates = []
        
        for page_num in range(min(max_pages, self.doc.page_count)):
            blocks = self.extract_text_blocks(page_num)
            if not blocks:
                continue
            
            # 提取页面文本
            full_text = " ".join([b.text for b in blocks])
            
            # 特征1：包含"目录"关键词
            has_toc_keyword = bool(re.search(r'目\s*录|contents|CONTENTS', full_text, re.I))
            
            # 特征2：多个点号引导线
            dot_leaders = len(re.findall(r'\.{3,}|···|…{2,}', full_text))
            
            # 特征3：多个右对齐数字（页码）
            right_side_numbers = 0
            for b in blocks:
                if re.match(r'^\d+$', b.text.strip()):
                    if b.x0 > self.page_width * 0.7:
                        right_side_numbers += 1
            
            # 特征4：缩进层级（通过x坐标差异判断）
            x_positions = [b.x0 for b in blocks if len(b.text) > 2]
            unique_x = len(set(round(x, 1) for x in x_positions)) if x_positions else 0
            
            # 特征5：标准章节标题模式
            chapter_patterns = [
                r'第[一二三四五六七八九十]+节',
                r'[一二三四五六七八九十]+、',
                r'（[一二三四五六七八九十]+）',
                r'\([一二三四五六七八九十]+\)',
                r'第[一二三四五六七八九十]+章',
                r'\d+\.\d+',
            ]
            chapter_matches = 0
            for pattern in chapter_patterns:
                chapter_matches += len(re.findall(pattern, full_text))
            
            # 特征6：无页码目录（章节号 + 标题 + 点号线，无页码数字）
            no_page_num_toc = len(re.findall(r'第[一二三四五六七八九十]+[章节].{2,20}?\.{3,}', full_text))
            
            # 综合评分
            score = 0.0
            if has_toc_keyword:
                score += 0.3
            if dot_leaders >= 3:
                score += 0.2
            if right_side_numbers >= 3:
                score += 0.2
            if unique_x >= 3:
                score += 0.1
            if chapter_matches >= 5:
                score += 0.2
            if no_page_num_toc >= 3:
                score += 0.3  # 无页码目录加分
            
            # 页面位置偏好（目录通常在P2-P10）
            if 1 <= page_num <= 10:
                score += 0.1
            
            if score >= 0.5:
                candidates.append((page_num, round(score, 2)))
        
        # 按置信度排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates
    
    def parse_toc_structure(self, page_num: int) -> List[TOCItem]:
        """
        解析目录页结构
        支持多种格式：
          1. 标准格式：标题 + 点号线 + 页码
          2. 无页码格式：章节号 + 标题 + 点号线（无页码）
          3. 三栏格式：大字体页码 + 小字体标题
        """
        blocks = self.extract_text_blocks(page_num)
        
        # 先尝试标准格式（标题 + 点号线 + 页码）
        items = self._parse_standard_toc(blocks)
        if len(items) >= 3:
            return items
        
        # 再尝试无页码格式（章节号 + 标题 + 点号线）
        items = self._parse_no_page_num_toc(blocks)
        if len(items) >= 3:
            return items
        
        # 最后尝试三栏格式
        items = self._parse_three_column_toc(blocks, page_num)
        return items
    
    def _parse_standard_toc(self, blocks: List[TextBlock]) -> List[TOCItem]:
        """
        解析标准目录格式：标题 + 点号线 + 页码
        支持多种变体：
          - "第一节 重要提示 ......................... 1"
          - "公司简介........................................... 8" (点号线和页码连在一起)
          - "第一节 重要提示 1" (无点号线)
        """
        items = []
        
        # 合并同一行的文本块
        lines = self._merge_blocks_to_lines(blocks)
        
        for line_text, line_blocks in lines:
            # 跳过页眉页脚和纯点号线
            if self._is_footer_text(line_text):
                continue
            if re.match(r'^[\.·…\s]+$', line_text.strip()):
                continue
            
            text = line_text.strip()
            
            # 跳过纯数字行（这些是页码，不是标题）
            if re.match(r'^\d+$', text):
                continue
            
            # 模式1: 标题 + 点号线(可选) + 页码
            # 例如："公司简介........................................... 8"
            # 例如："第一节 重要提示 .................... 1"
            match = re.match(r'(.+?)(?:\.{2,}|···|…+)?\s*(\d+)$', text)
            if match:
                title_part = match.group(1).strip()
                page_num = int(match.group(2))
                
                # 清理标题中的点号线残留
                title = re.sub(r'\.{2,}$', '', title_part).strip()
                title = re.sub(r'[·…]+$', '', title).strip()
                
                # 过滤无效标题
                if len(title) < 2 or title == '.':
                    continue
                # 过滤纯数字标题
                if re.match(r'^\d+$', title):
                    continue
                
                # 判断层级
                level = self._detect_level(title, line_blocks)
                
                items.append(TOCItem(
                    title=title,
                    page=page_num,
                    level=level,
                    raw_text=line_text
                ))
        
        # 如果没有提取到足够条目，尝试分行格式（页码和标题在不同行）
        if len(items) < 3:
            items = self._parse_multi_line_toc(lines)
        
        return items
    
    def _parse_multi_line_toc(self, lines: List[Tuple[str, List[TextBlock]]]) -> List[TOCItem]:
        """
        解析分行目录格式：
        页码、章节号、标题各占一行
        例如：
          04
          第一节 
          释义
          06
          第二节 
          公司简介和 
          主要财务指标
        """
        items = []
        
        i = 0
        while i < len(lines):
            line_text, line_blocks = lines[i]
            text = line_text.strip()
            
            # 跳过页眉页脚和垃圾
            if self._is_footer_text(text) or re.match(r'^[\.·…\s]+$', text):
                i += 1
                continue
            
            # 检测页码行：纯数字
            if re.match(r'^\d+$', text):
                page_num = int(text)
                
                # 向前看：收集后续的章节号和标题行
                j = i + 1
                chapter_num = ""
                title_parts = []
                
                while j < len(lines):
                    next_text, next_blocks = lines[j]
                    next_text = next_text.strip()
                    
                    # 如果遇到下一个页码，停止
                    if re.match(r'^\d+$', next_text):
                        break
                    
                    # 跳过垃圾
                    if self._is_footer_text(next_text) or re.match(r'^[\.·…\s]+$', next_text):
                        j += 1
                        continue
                    
                    # 检测章节号
                    if re.match(r'^第[一二三四五六七八九十]+节', next_text):
                        chapter_num = next_text
                    # 检测标题（非章节号、非页码的文本）
                    elif len(next_text) > 1 and not next_text.isdigit():
                        title_parts.append(next_text)
                    
                    j += 1
                
                # 构建完整标题
                if chapter_num and title_parts:
                    full_title = f"{chapter_num} {' '.join(title_parts)}"
                    level = self._detect_level(full_title, [])
                    
                    items.append(TOCItem(
                        title=full_title,
                        page=page_num,
                        level=level,
                        raw_text=full_title
                    ))
                elif title_parts:
                    # 没有章节号，只有标题
                    title = ' '.join(title_parts)
                    level = self._detect_level(title, [])
                    items.append(TOCItem(
                        title=title,
                        page=page_num,
                        level=level,
                        raw_text=title
                    ))
                
                i = j  # 跳到下一个页码行
            else:
                i += 1
        
        return items


    def _parse_multi_line_toc(self, lines: List[Tuple[str, List[TextBlock]]]) -> List[TOCItem]:
        """
        解析分行目录格式：
        页码、章节号、标题各占一行
        例如：
          04
          第一节 
          释义
          06
          第二节 
          公司简介和 
          主要财务指标
        """
        items = []
        
        i = 0
        while i < len(lines):
            line_text, line_blocks = lines[i]
            text = line_text.strip()
            
            # 跳过页眉页脚和垃圾
            if self._is_footer_text(text) or re.match(r'^[\.·…\s]+$', text):
                i += 1
                continue
            
            # 检测页码行：纯数字
            if re.match(r'^\d+$', text):
                page_num = int(text)
                
                # 向前看：收集后续的章节号和标题行
                j = i + 1
                chapter_num = ""
                title_parts = []
                
                while j < len(lines):
                    next_text, next_blocks = lines[j]
                    next_text = next_text.strip()
                    
                    # 如果遇到下一个页码，停止
                    if re.match(r'^\d+$', next_text):
                        break
                    
                    # 跳过垃圾
                    if self._is_footer_text(next_text) or re.match(r'^[\.·…\s]+$', next_text):
                        j += 1
                        continue
                    
                    # 检测章节号
                    if re.match(r'^第[一二三四五六七八九十]+节', next_text):
                        chapter_num = next_text
                    # 检测标题（非章节号、非页码的文本）
                    elif len(next_text) > 1 and not next_text.isdigit():
                        title_parts.append(next_text)
                    
                    j += 1
                
                # 构建完整标题
                if chapter_num and title_parts:
                    full_title = f"{chapter_num} {' '.join(title_parts)}"
                    level = self._detect_level(full_title, [])
                    
                    items.append(TOCItem(
                        title=full_title,
                        page=page_num,
                        level=level,
                        raw_text=full_title
                    ))
                elif title_parts:
                    # 没有章节号，只有标题
                    title = ' '.join(title_parts)
                    level = self._detect_level(title, [])
                    items.append(TOCItem(
                        title=title,
                        page=page_num,
                        level=level,
                        raw_text=title
                    ))
                
                i = j  # 跳到下一个页码行
            else:
                i += 1
        
        return items
    
    def _parse_no_page_num_toc(self, blocks: List[TextBlock]) -> List[TOCItem]:
        """
        解析无页码目录格式：章节号 + 标题 + 点号线
        例如："第一章 公司简介 ................."
        没有显式页码，页码需要在正文中搜索定位
        """
        items = []
        
        # 合并同一行的文本块
        lines = self._merge_blocks_to_lines(blocks)
        
        for line_text, line_blocks in lines:
            # 跳过页眉页脚和点号线本身
            if self._is_footer_text(line_text):
                continue
            if re.match(r'^[\.·…\s]+$', line_text):
                continue
            
            # 检测模式：章节号 + 标题
            # 例如："第一章 公司简介" 或 "第一节 重要提示"
            # 或："3.1 总体经营情况"
            match = re.match(r'^(第[一二三四五六七八九十]+[章节]|\d+\.\d+)[\s]+(.+?)(?:\.{2,}|···|…+)?$', line_text.strip())
            if match:
                chapter_num = match.group(1)
                title = match.group(2).strip()
                full_title = f"{chapter_num} {title}"
                
                level = self._detect_level(full_title, line_blocks)
                
                items.append(TOCItem(
                    title=full_title,
                    page=-1,  # 无页码，需要后续定位
                    level=level,
                    raw_text=line_text
                ))
            else:
                # 尝试检测只有标题的情况（如"重要提示"、"备查文件目录"）
                # 这些通常是没有章节号的前置部分
                if len(line_text) > 2 and len(line_text) < 50:
                    # 检查是否是常见的独立标题
                    standalone_titles = ['重要提示', '备查文件目录', '行长致辞', '致辞',
                                        '目录', '释义', '公司简介']
                    if any(t in line_text for t in standalone_titles):
                        level = 0
                        items.append(TOCItem(
                            title=line_text.strip(),
                            page=-1,
                            level=level,
                            raw_text=line_text
                        ))
        
        return items
    
    def _merge_blocks_to_lines(self, blocks: List[TextBlock]) -> List[Tuple[str, List[TextBlock]]]:
        """
        将文本块按行合并
        同一行的y坐标差异不超过容差值
        支持多栏布局：如果x坐标差异过大，分成不同逻辑行
        """
        if not blocks:
            return []
        
        y_tolerance = 8  # 像素容差
        x_gap_threshold = 200  # 栏间距阈值（三栏布局需要更大的阈值）
        
        # 按y坐标排序
        sorted_blocks = sorted(blocks, key=lambda b: (b.y0, b.x0))
        
        lines = []
        current_line = [sorted_blocks[0]]
        current_y = sorted_blocks[0].y0
        
        for block in sorted_blocks[1:]:
            if abs(block.y0 - current_y) <= y_tolerance:
                current_line.append(block)
            else:
                # 按x坐标排序当前行
                current_line.sort(key=lambda b: b.x0)
                
                # 多栏布局检测：如果相邻块的x差距太大，分成多行
                sub_lines = self._split_multi_column_line(current_line, x_gap_threshold)
                lines.extend(sub_lines)
                
                current_line = [block]
                current_y = block.y0
        
        # 处理最后一行
        if current_line:
            current_line.sort(key=lambda b: b.x0)
            sub_lines = self._split_multi_column_line(current_line, x_gap_threshold)
            lines.extend(sub_lines)
        
        return lines
    
    def _split_multi_column_line(self, blocks: List[TextBlock], x_gap_threshold: float) -> List[Tuple[str, List[TextBlock]]]:
        """
        将多栏布局的一行分成多个逻辑行
        """
        if not blocks:
            return []
        
        sub_lines = []
        current_sub = [blocks[0]]
        
        for block in blocks[1:]:
            prev_block = current_sub[-1]
            x_gap = block.x0 - prev_block.x1
            
            if x_gap > x_gap_threshold:
                # 新的栏，开始新的逻辑行
                line_text = ' '.join([b.text for b in current_sub])
                sub_lines.append((line_text, current_sub))
                current_sub = [block]
            else:
                current_sub.append(block)
        
        # 处理最后一个子行
        if current_sub:
            line_text = ' '.join([b.text for b in current_sub])
            sub_lines.append((line_text, current_sub))
        
        return sub_lines


    def _split_multi_column_line(self, blocks: List[TextBlock], x_gap_threshold: float) -> List[Tuple[str, List[TextBlock]]]:
        """
        将多栏布局的一行分成多个逻辑行
        """
        if not blocks:
            return []
        
        sub_lines = []
        current_sub = [blocks[0]]
        
        for block in blocks[1:]:
            prev_block = current_sub[-1]
            x_gap = block.x0 - prev_block.x1
            
            if x_gap > x_gap_threshold:
                # 新的栏，开始新的逻辑行
                line_text = ' '.join([b.text for b in current_sub])
                sub_lines.append((line_text, current_sub))
                current_sub = [block]
            else:
                current_sub.append(block)
        
        # 处理最后一个子行
        if current_sub:
            line_text = ' '.join([b.text for b in current_sub])
            sub_lines.append((line_text, current_sub))
        
        return sub_lines
    
    def _detect_level(self, title: str, blocks: List[TextBlock]) -> int:
        """检测目录条目的层级"""
        # 方法1：通过标题前缀判断（优先）
        if re.match(r'^第[一二三四五六七八九十]+章', title):
            return 0
        if re.match(r'^第[一二三四五六七八九十]+节', title):
            return 0
        if re.match(r'^\d+\.\d+', title):
            return 1
        if re.match(r'^[一二三四五六七八九十]+、', title):
            return 1
        if re.match(r'^（[一二三四五六七八九十]+）', title):
            return 2
        
        # 方法2：通过缩进判断（辅助，仅当前缀无法判断时）
        if blocks:
            # 只保留看起来是标题的块
            title_blocks = [b for b in blocks 
                           if not re.match(r'^\d+$', b.text.strip())
                           and not re.match(r'^[\.·…\s]+$', b.text.strip())
                           and len(b.text.strip()) > 1]
            
            if title_blocks:
                avg_x = sum(b.x0 for b in title_blocks) / len(title_blocks)
                # 使用相对缩进：与页面宽度比较
                # 注意：需要参考同一页面中其他条目的缩进，这里简化处理
                if avg_x > self.page_width * 0.20:  # 相对缩进 > 20%页面宽度
                    return 1
                if avg_x > self.page_width * 0.30:
                    return 2
        
        return 0
    
    def _parse_three_column_toc(self, blocks: List[TextBlock], page_num: int) -> List[TOCItem]:
        """
        解析三栏布局目录
        特征：大字体数字(页码) + 小字体文字(标题)
        """
        # 分离页码块和标题块
        page_num_blocks = []  # 大字体数字
        title_blocks = []     # 小字体文字
        
        for b in blocks:
            text = b.text.strip()
            if not text:
                continue
                
            # 判断是页码还是标题
            is_pure_num = re.match(r'^\d+$', text)
            is_large_font = b.size > 20
            
            if is_pure_num and is_large_font:
                page_num_blocks.append(b)
            elif len(text) > 1 and not text.isdigit():
                # 过滤掉页眉页脚和垃圾
                if not self._is_footer_text(text):
                    title_blocks.append(b)
        
        # 按y坐标分组：将y坐标接近的页码和标题配对
        y_tolerance = 15
        
        page_num_blocks.sort(key=lambda b: b.y0)
        
        toc_items = []
        used_titles = set()
        
        for pn_block in page_num_blocks:
            page_num_val = int(pn_block.text.strip())
            pn_y = (pn_block.y0 + pn_block.y1) / 2
            
            best_title = None
            best_dist = float('inf')
            
            for t_block in title_blocks:
                t_key = f"{t_block.text}:{t_block.y0}"
                if t_key in used_titles:
                    continue
                
                t_y = (t_block.y0 + t_block.y1) / 2
                dist = abs(t_y - pn_y)
                
                if dist < y_tolerance and dist < best_dist:
                    best_dist = dist
                    best_title = t_block
            
            if best_title:
                t_key = f"{best_title.text}:{best_title.y0}"
                used_titles.add(t_key)
                
                level = self._detect_level(best_title.text, [best_title])
                toc_items.append(TOCItem(
                    title=best_title.text,
                    page=page_num_val,
                    level=level,
                    raw_text=best_title.text
                ))
        
        return toc_items
    
    def locate_chapters_in_text(self, toc_items: List[TOCItem], start_search_page: int = 0) -> List[TOCItem]:
        """
        对于无页码的目录项，在正文中搜索标题定位页码
        """
        for item in toc_items:
            if item.page > 0:
                continue  # 已有页码，跳过
            
            # 在正文中搜索标题
            found_page = self._search_title_in_pages(item.title, start_search_page)
            if found_page > 0:
                item.page = found_page
        
        return toc_items
    
    def _search_title_in_pages(self, title: str, start_page: int = 0, max_pages: int = 100) -> int:
        """
        在页面中搜索标题文本
        返回找到的页码，未找到返回-1
        """
        # 提取标题关键词（去掉章节号）
        keywords = re.sub(r'^第[一二三四五六七八九十]+[章节]\s*', '', title)
        keywords = re.sub(r'^\d+\.\d+\s*', '', keywords)
        keywords = keywords.strip()
        
        # 如果标题太短，用完整标题
        if len(keywords) < 3:
            keywords = title
        
        end_page = min(start_page + max_pages, self.doc.page_count)
        
        for page_num in range(start_page, end_page):
            page = self.doc[page_num]
            text = page.get_text()
            
            # 策略1：精确匹配前10个字
            title_prefix = title[:10]
            if title_prefix in text:
                return page_num + 1  # 返回1-based页码
            
            # 策略2：关键词匹配
            if keywords and len(keywords) >= 4 and keywords in text:
                return page_num + 1
            
            # 策略3：模糊匹配（去掉空格和标点）
            title_normalized = re.sub(r'[\s、，\.]', '', title[:15])
            text_normalized = re.sub(r'[\s、，\.]', '', text[:500])  # 只检查页面开头
            if title_normalized in text_normalized:
                return page_num + 1
        
        return -1
    
    def calculate_chapter_boundaries(self, toc_items: List[TOCItem]) -> List[Chapter]:
        """
        根据目录项计算章节边界
        """
        if not toc_items:
            return []
        
        chapters = []
        total_pages = self.doc.page_count
        
        for i, item in enumerate(toc_items):
            if item.page <= 0:
                continue  # 跳过未定位的章节
            
            start_page = item.page
            
            # 确定结束页：下一个同级或更高级章节的开始页-1
            end_page = total_pages
            for j in range(i + 1, len(toc_items)):
                next_item = toc_items[j]
                if next_item.page > 0 and next_item.level <= item.level:
                    end_page = next_item.page - 1
                    break
            
            # 边界保护
            end_page = max(start_page, min(end_page, total_pages))
            
            chapters.append(Chapter(
                title=item.title,
                start_page=start_page,
                end_page=end_page,
                level=item.level,
                toc_item=item
            ))
        
        return chapters
    
    def verify_chapters(self, chapters: List[Chapter]) -> Dict:
        """
        验证章节定位是否准确
        在章节起始页搜索标题文本
        """
        verification = {
            "verified": 0,
            "not_found": 0,
            "details": []
        }
        
        for ch in chapters:
            page_idx = ch.start_page - 1
            if page_idx < 0 or page_idx >= self.doc.page_count:
                continue
            
            page = self.doc[page_idx]
            page_text = page.get_text()
            
            # 多策略验证
            found = False
            
            # 策略1：标题前10个字在页面中
            title_prefix = ch.title[:10]
            if title_prefix in page_text:
                found = True
            
            # 策略2：标题前6个字（去掉章节号）
            if not found:
                clean_title = re.sub(r'^第[一二三四五六七八九十]+[章节]\s*', '', ch.title)
                clean_title = re.sub(r'^\d+\.\d+\s*', '', clean_title)
                if clean_title[:6] in page_text:
                    found = True
            
            # 策略3：关键词匹配
            if not found:
                keywords = [w for w in ch.title.split() if len(w) >= 2]
                if keywords and keywords[0] in page_text:
                    found = True
            
            ch.verified = found
            
            if found:
                verification["verified"] += 1
            else:
                verification["not_found"] += 1
            
            verification["details"].append({
                "title": ch.title,
                "page": ch.start_page,
                "found": found,
                "preview": page_text[:100].replace('\n', ' ')
            })
        
        return verification
    
    def slice_pdf_by_chapters(self, chapters: List[Chapter], output_prefix: str = "chapter") -> List[str]:
        """
        按章节切片PDF
        返回生成的文件路径列表
        """
        output_files = []
        
        for i, ch in enumerate(chapters):
            # 创建新PDF
            new_doc = fitz.open()
            
            # 插入页面范围
            start_idx = ch.start_page - 1
            end_idx = ch.end_page - 1
            
            for page_idx in range(start_idx, end_idx + 1):
                if 0 <= page_idx < self.doc.page_count:
                    new_doc.insert_pdf(self.doc, from_page=page_idx, to_page=page_idx)
            
            # 保存
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', ch.title[:30])
            output_path = f"{output_prefix}_{i+1:02d}_{safe_title}.pdf"
            new_doc.save(output_path)
            new_doc.close()
            
            output_files.append(output_path)
        
        return output_files


def extract_printed_page_numbers(extractor: PDFStructureExtractor) -> Dict[int, int]:
    """
    提取PDF中的印刷页码（页眉/页脚中的数字）
    使用位置信息和连续性检查来提高准确性
    返回: {物理页码(0-based): 印刷页码}
    """
    candidates = []  # [(物理页码, 印刷页码, 置信度), ...]
    
    for page_idx in range(extractor.doc.page_count):
        page = extractor.doc[page_idx]
        text = page.get_text()
        lines = text.split('\n')
        
        # 策略1：检查页面底部区域（页脚）的数字
        # 页码通常在页面底部居中或居右
        footer_lines = lines[-5:]  # 最后5行
        for line in footer_lines:
            line = line.strip()
            if line.isdigit():
                num = int(line)
                if 0 < num < 1000:
                    # 页脚位置的数字置信度较高
                    candidates.append((page_idx, num, 2.0))
                    break
        
        # 策略2：检查页面顶部区域（页眉）的数字
        # 如果页脚没有找到，检查页眉
        if not any(c[0] == page_idx for c in candidates):
            header_lines = lines[:3]  # 前3行
            for line in header_lines:
                line = line.strip()
                if line.isdigit():
                    num = int(line)
                    if 0 < num < 1000:
                        # 页眉位置的数字置信度较低（可能是章节编号）
                        candidates.append((page_idx, num, 1.0))
                        break
    
    # 连续性检查：找出最长的连续页码序列
    # 按物理页码排序
    candidates.sort(key=lambda x: x[0])
    
    # 找出最佳连续序列
    best_pages = {}
    best_length = 0
    
    for start in range(len(candidates)):
        pages = {}
        prev_phys = None
        prev_printed = None
        
        for i in range(start, len(candidates)):
            phys, printed, conf = candidates[i]
            
            # 检查连续性
            if prev_phys is not None:
                phys_gap = phys - prev_phys
                printed_gap = printed - prev_printed
                
                # 物理页码和印刷页码应该同步增长
                # 允许少量跳跃（如空白页、插图页）
                if phys_gap > 5 or printed_gap > 5 or printed_gap < 0:
                    break
            
            pages[phys] = printed
            prev_phys = phys
            prev_printed = printed
        
        if len(pages) > best_length:
            best_length = len(pages)
            best_pages = pages
    
    # 如果找到足够长的连续序列，使用它
    if best_length >= 5:
        return best_pages
    
    # 否则返回所有候选（降级处理）
    return {phys: printed for phys, printed, _ in candidates}


def build_page_mapping(extractor: PDFStructureExtractor) -> Dict[int, int]:
    """
    建立印刷页码到物理页码的映射
    返回: {印刷页码: 物理页码(0-based)}
    """
    printed = extract_printed_page_numbers(extractor)
    mapping = {printed_num: phys_idx for phys_idx, printed_num in printed.items()}
    return mapping


def analyze_pdf_structure(pdf_path: str) -> Dict:
    """
    分析PDF结构的主函数
    """
    extractor = PDFStructureExtractor(pdf_path)
    
    result = {
        "toc_pages": [],
        "total_pages": extractor.doc.page_count,
        "chapters": [],
        "verification": {}
    }
    
    # 0. 建立页码映射
    page_mapping = build_page_mapping(extractor)
    print(f"  提取到 {len(page_mapping)} 个印刷页码映射")
    
    # 1. 查找目录页
    toc_candidates = extractor.find_toc_pages(max_pages=50)
    if not toc_candidates:
        result["error"] = "未找到目录页"
        return result
    
    toc_page, confidence = toc_candidates[0]
    result["toc_pages"] = [toc_page + 1]  # 转为1-based
    
    # 2. 解析目录结构
    toc_items = extractor.parse_toc_structure(toc_page)
    
    # 3. 转换页码：印刷页码 -> 物理页码
    for item in toc_items:
        if item.page > 0 and item.page in page_mapping:
            item.page = page_mapping[item.page] + 1  # 转为1-based物理页码
        elif item.page > 0:
            # 如果映射中没有，尝试估算
            # 找到最接近的印刷页码
            closest = min(page_mapping.keys(), key=lambda k: abs(k - item.page))
            offset = page_mapping[closest] - closest
            item.page = item.page + offset + 1
    
    # 4. 对于仍未定位的项，在正文中搜索
    unlocated = [item for item in toc_items if item.page <= 0]
    if unlocated:
        toc_items = extractor.locate_chapters_in_text(toc_items, start_search_page=toc_page + 5)
    
    # 5. 计算章节边界
    chapters = extractor.calculate_chapter_boundaries(toc_items)
    
    # 6. 验证
    verification = extractor.verify_chapters(chapters)
    
    # 构建结果
    result["chapters"] = [
        {
            "title": ch.title,
            "start_page": ch.start_page,
            "end_page": ch.end_page,
            "level": ch.level,
            "verified": ch.verified
        }
        for ch in chapters
    ]
    result["verification"] = verification
    
    return result


def main():
    import sys
    
    if len(sys.argv) < 2:
        print("用法: python3 pdf_toc_slicer.py <pdf文件路径>")
        print("\n示例:")
        print("  python3 pdf_toc_slicer.py 年报.pdf")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    
    print("=" * 60)
    print(f"分析: {pdf_path}")
    print("=" * 60)
    
    extractor = PDFStructureExtractor(pdf_path)
    
    # 1. 查找目录页
    print("\n[1/4] 查找目录页...")
    toc_candidates = extractor.find_toc_pages(max_pages=50)
    if not toc_candidates:
        print("  ❌ 未找到目录页")
        return
    
    for page_num, conf in toc_candidates[:3]:
        print(f"  页面 {page_num + 1}: 目录候选 (置信度 {conf:.2f})")
    
    toc_page = toc_candidates[0][0]
    print(f"  找到 {len(toc_candidates)} 个目录页候选")
    
    # 2. 解析目录结构
    print("\n[2/4] 解析目录结构...")
    toc_items = extractor.parse_toc_structure(toc_page)
    
    # 2.5 建立页码映射并转换
    print("\n  建立页码映射...")
    page_mapping = build_page_mapping(extractor)
    print(f"  提取到 {len(page_mapping)} 个印刷页码映射")
    
    converted = 0
    for item in toc_items:
        if item.page > 0 and item.page in page_mapping:
            item.page = page_mapping[item.page] + 1  # 转为1-based物理页码
            converted += 1
        elif item.page > 0:
            # 估算
            closest = min(page_mapping.keys(), key=lambda k: abs(k - item.page))
            offset = page_mapping[closest] - closest
            item.page = item.page + offset + 1
            converted += 1
    print(f"  转换 {converted} 个页码")
    
    # 显示转换后的结果
    print(f"\n  页面 {toc_page + 1}: 提取 {len(toc_items)} 个条目")
    
    if toc_items:
        print("\n  目录结构:")
        for item in toc_items[:20]:  # 显示前20个
            indent = "  " * item.level
            page_str = f"(P{item.page})" if item.page > 0 else "(待定位)"
            print(f"    {indent}└─ {item.title} {page_str}")
        
        if len(toc_items) > 20:
            print(f"    ... 还有 {len(toc_items) - 20} 项")
    
    # 3. 定位无页码的章节
    unlocated = [item for item in toc_items if item.page <= 0]
    if unlocated:
        print(f"\n  定位 {len(unlocated)} 个无页码章节...")
        toc_items = extractor.locate_chapters_in_text(toc_items, start_search_page=toc_page + 5)
        
        still_unlocated = [item for item in toc_items if item.page <= 0]
        if still_unlocated:
            print(f"  仍有 {len(still_unlocated)} 个章节未定位")
    
    # 4. 计算章节边界
    print("\n[3/4] 确定章节边界...")
    chapters = extractor.calculate_chapter_boundaries(toc_items)
    print(f"  识别 {len(chapters)} 个章节:")
    
    for ch in chapters:
        indent = "  " * ch.level
        print(f"    {indent}└─ {ch.title}: P{ch.start_page}-P{ch.end_page}")
    
    # 5. 验证
    print("\n[4/4] 验证章节定位...")
    verification = extractor.verify_chapters(chapters)
    
    verified = verification["verified"]
    not_found = verification["not_found"]
    total = verified + not_found
    
    if total > 0:
        rate = verified / total * 100
        print(f"  验证率: {verified}/{total} ({rate:.1f}%)")
    
    # 输出JSON报告
    print("\n" + "=" * 60)
    print("分析报告:")
    print("=" * 60)
    
    result = {
        "toc_pages": [p + 1 for p, _ in toc_candidates[:3]],
        "total_pages": extractor.doc.page_count,
        "chapters": [
            {
                "title": ch.title,
                "start_page": ch.start_page,
                "end_page": ch.end_page,
                "level": ch.level,
                "verified": ch.verified
            }
            for ch in chapters
        ],
        "verification": {
            "verified": verified,
            "not_found": not_found,
            "rate": verified / total * 100 if total > 0 else 0
        }
    }
    
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 切片示例（可选）
    if chapters and len(sys.argv) > 2 and sys.argv[2] == "--slice":
        print("\n[切片] 生成章节PDF...")
        output_files = extractor.slice_pdf_by_chapters(chapters, "chapter")
        print(f"  生成 {len(output_files)} 个文件:")
        for f in output_files:
            print(f"    - {f}")


if __name__ == "__main__":
    main()
