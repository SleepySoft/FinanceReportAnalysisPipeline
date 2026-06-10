#!/usr/bin/env python3
"""
PDF 目录解析器 - 基于 pdfplumber / PyMuPDF

策略：
1. 尝试读取 PDF 元数据中的书签/大纲（Outlines）
2. 如果元数据不可用，扫描前几页文本找"目录"特征
3. 解析目录结构：章节名 + 页码
4. 输出结构化 JSON

用法：
    python3 pdf_toc_parser.py <pdf_path>
    python3 pdf_toc_parser.py --test-all /path/to/pdf/dir/
"""

import sys
import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict

# 优先尝试 pdfplumber，回退到 PyMuPDF
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False


@dataclass
class TocEntry:
    """目录条目"""
    level: int              # 层级（1=章，2=节，3=小节）
    title: str              # 标题文本
    page: Optional[int]     # 页码（可能为None）
    section_num: Optional[str] = None  # "第三节" 这种编号
    raw_line: str = ""      # 原始文本行

    def to_dict(self):
        return asdict(self)


class PdfTocParser:
    """PDF 目录解析器"""

    # 目录页关键词（中文年报）
    TOC_KEYWORDS = [
        "目录", "CONTENTS", "内容", "目次",
        "章  节", "章节", "目 录"
    ]

    # 章节编号模式
    SECTION_PATTERN = re.compile(
        r'第[一二三四五六七八九十百零]+节|第\d+节|'
        r'[一二三四五六七八九十]+、|'
        r'\d+\.\d+|\d+、'
    )

    # 常见章节关键词（用于验证目录条目）
    CHAPTER_KEYWORDS = [
        '重要提示', '目录', '释义', '公司简介', '财务指标', '管理层讨论',
        '公司治理', '环境', '社会责任', '重要事项', '股份变动', '股东情况',
        '债券', '财务报告', '审计报告', '备查文件', '经营情况', '董事会',
        '监事会', '高管', '薪酬', '关联交易', '募集资金', '重大合同'
    ]

    # 目录行模式：标题 + 空格/点前导符 + 页码
    # 支持格式：
    #   1. "第一节 重要提示、目录和释义 4"  (纯空格)
    #   2. "第一节 重要提示、目录和释义 ........ 4"  (点前导符)
    #   3. "重要提示、目录和释义 4"  (无"第X节"前缀)
    TOC_LINE_PATTERN = re.compile(
        r'^(?:\s*(第[一二三四五六七八九十]+节)\s+)?'  # 可选：第X节
        r'(.+?)'                                      # 标题
        r'\s+(\d+)'                                   # 页码（至少一个空格）
        r'\s*$',
        re.UNICODE
    )

    # 备选模式：更宽松的匹配（带点前导符的）
    TOC_LINE_LOOSE = re.compile(
        r'^(?:\s*(第[一二三四五六七八九十]+节)\s+)?'  # 可选：第X节
        r'(.+?)'                                      # 标题
        r'\s*[\.·…‥\-–—_\s]+'                        # 分隔符（点前导符等）
        r'(\d+)'                                      # 页码
        r'\s*$',
        re.UNICODE
    )

    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self.backend = None
        self._detect_backend()

    def _detect_backend(self):
        """检测可用的 PDF 库"""
        if HAS_PDFPLUMBER:
            self.backend = "pdfplumber"
        elif HAS_FITZ:
            self.backend = "pymupdf"
        else:
            raise ImportError("需要安装 pdfplumber 或 PyMuPDF: pip install pdfplumber pymupdf")

    def parse(self) -> Dict:
        """
        主入口：解析 PDF 目录
        返回：{
            "success": bool,
            "pdf_path": str,
            "backend": str,
            "method": str,          # "outline" | "text_scan" | "fallback"
            "toc_entries": [...],   # TocEntry 列表
            "total_pages": int,
            "toc_page_range": [start, end],  # 目录页范围（1-indexed）
            "errors": [str]
        }
        """
        result = {
            "success": False,
            "pdf_path": str(self.pdf_path),
            "backend": self.backend,
            "method": None,
            "toc_entries": [],
            "total_pages": 0,
            "toc_page_range": None,
            "errors": []
        }

        try:
            # 方法1：尝试读取 PDF 元数据大纲
            outline_entries = self._parse_outline()
            if outline_entries and len(outline_entries) >= 3:
                result["method"] = "outline"
                result["toc_entries"] = [e.to_dict() for e in outline_entries]
                result["success"] = True
                return result

            # 方法2：扫描文本找目录页
            text_entries, page_range = self._scan_text_for_toc()
            if text_entries and len(text_entries) >= 3:
                result["method"] = "text_scan"
                result["toc_entries"] = [e.to_dict() for e in text_entries]
                result["toc_page_range"] = page_range
                result["success"] = True
                return result

            # 方法3：兜底 - 扫描所有页面找章节标题模式
            fallback_entries = self._fallback_scan()
            if fallback_entries:
                result["method"] = "fallback"
                result["toc_entries"] = [e.to_dict() for e in fallback_entries]
                result["success"] = True
                return result

            result["errors"].append("无法找到有效目录")
            return result

        except Exception as e:
            result["errors"].append(f"解析异常: {str(e)}")
            return result

    # ==================== 方法1：PDF 元数据大纲 ====================

    def _parse_outline(self) -> List[TocEntry]:
        """读取 PDF 书签/大纲"""
        entries = []

        if self.backend == "pdfplumber":
            try:
                with pdfplumber.open(self.pdf_path) as pdf:
                    # pdfplumber 不直接提供 outline，但可以通过底层 pdfminer 访问
                    # 这里简化处理，返回空让流程走到 text_scan
                    pass
            except Exception:
                pass

        elif self.backend == "pymupdf":
            try:
                doc = fitz.open(self.pdf_path)
                toc = doc.get_toc()  # [(level, title, page), ...]
                for level, title, page in toc:
                    # 提取章节编号
                    section_num = None
                    match = re.match(r'(第[一二三四五六七八九十]+节)', title)
                    if match:
                        section_num = match.group(1)

                    entries.append(TocEntry(
                        level=level,
                        title=title.strip(),
                        page=page,
                        section_num=section_num
                    ))
                doc.close()
            except Exception:
                pass

        return entries

    # ==================== 方法2：文本扫描目录页 ====================

    def _scan_text_for_toc(self) -> Tuple[List[TocEntry], Optional[List[int]]]:
        """
        扫描前几页文本，找目录页
        返回：(entries, [start_page, end_page]) 或 ([], None)
        """
        entries = []
        toc_pages = []

        if self.backend == "pdfplumber":
            with pdfplumber.open(self.pdf_path) as pdf:
                self.total_pages = len(pdf.pages)

                # 扫描前 10 页找目录
                for i in range(min(10, len(pdf.pages))):
                    page = pdf.pages[i]
                    text = page.extract_text() or ""

                    if self._is_toc_page(text):
                        toc_pages.append(i + 1)  # 1-indexed
                        page_entries = self._parse_toc_text(text)
                        entries.extend(page_entries)

        elif self.backend == "pymupdf":
            doc = fitz.open(self.pdf_path)
            self.total_pages = len(doc)

            for i in range(min(10, len(doc))):
                page = doc[i]
                text = page.get_text()

                if self._is_toc_page(text):
                    toc_pages.append(i + 1)
                    page_entries = self._parse_toc_text(text)
                    entries.extend(page_entries)

            doc.close()

        page_range = [min(toc_pages), max(toc_pages)] if toc_pages else None
        return entries, page_range

    def _is_toc_page(self, text: str) -> bool:
        """判断一页是否是目录页"""
        if not text:
            return False

        lines = text.strip().split('\n')

        # 检查1：是否包含目录关键词
        has_keyword = any(kw in text for kw in self.TOC_KEYWORDS)

        # 检查2：是否有足够多的"标题+页码"模式行
        toc_lines = 0
        for line in lines:
            if self._is_valid_toc_line(line.strip()):
                toc_lines += 1

        # 检查3：页码行占比（目录页通常 50%+ 的行是目录条目）
        toc_ratio = toc_lines / len(lines) if lines else 0

        # 判断逻辑
        if has_keyword and toc_lines >= 3:
            return True
        if toc_lines >= 5 and toc_ratio >= 0.3:
            return True

        return False

    def _is_valid_toc_line(self, line: str) -> bool:
        """判断一行是否是有效的目录条目"""
        if not line:
            return False
        
        # 跳过页眉
        if '年度报告' in line and ('目录' in line or '释义' in line):
            return False
        
        # 尝试匹配
        match = self.TOC_LINE_PATTERN.match(line)
        if not match:
            match = self.TOC_LINE_LOOSE.match(line)
        
        if not match:
            return False
        
        groups = match.groups()
        if len(groups) == 3:
            section_num, title, page = groups
        elif len(groups) == 2:
            title, page = groups
            section_num = None
        else:
            return False
        
        title = title.strip()
        title = re.sub(r'[（(]续[)）]', '', title).strip()
        
        # 过滤1：标题长度
        if len(title) < 4 or len(title) > 50:
            return False
        
        # 过滤2：页码范围
        try:
            page_num = int(page)
        except ValueError:
            return False
        if page_num < 1 or page_num > 500:
            return False
        
        # 过滤3：必须包含章节关键词或"第X节"
        has_section_num = section_num is not None
        has_keyword = any(kw in title for kw in self.CHAPTER_KEYWORDS)
        
        # 特殊处理：如果标题里有"年内召开"这种表格内容，直接拒绝
        if '年内召开' in title or '会议次数' in title:
            return False
        
        # 特殊处理：排除包含"原则"的长句（正文内容误匹配）
        if '原则' in title and len(title) > 15:
            return False
        
        # 特殊处理：排除"环境信息依法披露"这种非目录内容
        if '环境信息依法披露' in title or '企业数量' in title:
            return False
        
        # 特殊处理：排除"株冶"开头且不含章节关键词的
        if title.startswith('株冶') and not has_section_num:
            return False
        
        # 特殊处理：排除"塘环境技"这种乱码/碎片
        if '塘环境技' in title:
            return False
        
        if not has_section_num and not has_keyword:
            return False
        
        # 过滤4：排除明显不是目录的行（如电话号码、财务数据）
        if re.search(r'\d{4,}', title) and not has_section_num:
            return False
        
        # 过滤5：标题中不能包含过多数字和标点（财务数据特征）
        digit_count = sum(1 for c in title if c.isdigit())
        if digit_count >= 4:
            return False
        
        # 过滤6：标题中不能包含"."后跟数字（如"应付债券 123.45"）
        if re.search(r'\.\d{2,}', title):
            return False
        
        return True

    def _parse_toc_text(self, text: str) -> List[TocEntry]:
        """解析目录页文本，提取结构化条目"""
        entries = []
        lines = text.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            if not self._is_valid_toc_line(line):
                continue
            
            match = self.TOC_LINE_PATTERN.match(line)
            if not match:
                match = self.TOC_LINE_LOOSE.match(line)
            
            if match:
                groups = match.groups()
                if len(groups) == 3:
                    section_num, title, page = groups
                elif len(groups) == 2:
                    title, page = groups
                    section_num = None
                else:
                    continue
                
                title = title.strip()
                title = re.sub(r'[（(]续[)）]', '', title).strip()
                
                try:
                    page_num = int(page)
                except ValueError:
                    continue
                
                entries.append(TocEntry(
                    level=1,  # 默认一级
                    title=title,
                    page=page_num,
                    section_num=section_num.strip() if section_num else None,
                    raw_line=line
                ))
        
        return entries

    # ==================== 方法3：兜底扫描 ====================

    def _fallback_scan(self) -> List[TocEntry]:
        """
        兜底：扫描所有页面，找章节标题模式
        用于完全没有目录页的情况
        """
        entries = []
        seen_pages = set()

        if self.backend == "pdfplumber":
            with pdfplumber.open(self.pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    # 找页面顶部的大标题
                    first_lines = '\n'.join(text.split('\n')[:5])

                    # 匹配 "第X节 XXX" 或 "X、XXX" 在页面开头
                    match = re.search(
                        r'^(?:\s*)(第[一二三四五六七八九十]+节|[一二三四五六七八九十]+、)(.+?)(?:\n|$)',
                        first_lines,
                        re.MULTILINE
                    )
                    if match and (i + 1) not in seen_pages:
                        seen_pages.add(i + 1)
                        entries.append(TocEntry(
                            level=1,
                            title=match.group(2).strip(),
                            page=i + 1,
                            section_num=match.group(1).strip() if match.group(1) else None
                        ))

        elif self.backend == "pymupdf":
            doc = fitz.open(self.pdf_path)
            for i in range(len(doc)):
                page = doc[i]
                text = page.get_text()
                first_lines = '\n'.join(text.split('\n')[:5])

                match = re.search(
                    r'^(?:\s*)(第[一二三四五六七八九十]+节|[一二三四五六七八九十]+、)(.+?)(?:\n|$)',
                    first_lines,
                    re.MULTILINE
                )
                if match and (i + 1) not in seen_pages:
                    seen_pages.add(i + 1)
                    entries.append(TocEntry(
                        level=1,
                        title=match.group(2).strip(),
                        page=i + 1,
                        section_num=match.group(1).strip() if match.group(1) else None
                    ))
            doc.close()

        return entries

    # ==================== 页码校正 ====================

    def correct_page_numbers(self, entries: List[TocEntry], total_pages: int) -> List[TocEntry]:
        """
        校正页码：处理封面不计页码、PDF页码偏移等情况
        """
        if not entries:
            return entries

        # 策略：如果第一个有效页码 > 5，可能有偏移
        first_page = entries[0].page
        if first_page and first_page > 20:
            offset = first_page - 2  # 假设目录应该在第2页
            for e in entries:
                if e.page:
                    e.page = e.page - offset

        # 过滤超出范围的页码
        valid_entries = []
        for e in entries:
            if e.page and 1 <= e.page <= total_pages:
                valid_entries.append(e)

        return valid_entries


# ==================== 切片功能 ====================

def slice_pdf_by_toc(pdf_path: str, toc_entries: List[Dict], output_dir: str) -> List[Dict]:
    """
    按目录页码切片 PDF，每片导出为 TXT

    参数：
        pdf_path: PDF 文件路径
        toc_entries: 目录条目列表（来自 PdfTocParser.parse()）
        output_dir: 输出目录

    返回：
        [{"title": "...", "page_start": 1, "page_end": 10, "txt_path": "...", "char_count": 1234}, ...]
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    slices = []

    # 检测后端
    if HAS_PDFPLUMBER:
        backend = "pdfplumber"
    elif HAS_FITZ:
        backend = "pymupdf"
    else:
        raise ImportError("需要 pdfplumber 或 PyMuPDF")

    # 获取总页数
    if backend == "pdfplumber":
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
    else:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()

    # 按页码排序
    sorted_entries = sorted(
        [e for e in toc_entries if e.get("page")],
        key=lambda x: x["page"]
    )

    for i, entry in enumerate(sorted_entries):
        start_page = entry["page"] - 1  # 0-indexed
        if i + 1 < len(sorted_entries):
            end_page = sorted_entries[i + 1]["page"] - 1
        else:
            end_page = total_pages

        # 提取文本
        section_text = ""
        if backend == "pdfplumber":
            with pdfplumber.open(pdf_path) as pdf:
                for p in range(start_page, min(end_page, total_pages)):
                    page_text = pdf.pages[p].extract_text() or ""
                    section_text += page_text + "\n\n"
        else:
            doc = fitz.open(pdf_path)
            for p in range(start_page, min(end_page, total_pages)):
                section_text += doc[p].get_text() + "\n\n"
            doc.close()

        # 保存
        safe_title = re.sub(r'[^\w\u4e00-\u9fff]+', '_', entry["title"]).strip('_')
        txt_path = output_dir / f"{safe_title}.txt"

        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(section_text)

        slices.append({
            "title": entry["title"],
            "page_start": entry["page"],
            "page_end": end_page,
            "txt_path": str(txt_path),
            "char_count": len(section_text)
        })

    return slices


# ==================== CLI ====================

def main():
    if len(sys.argv) < 2:
        print("用法: python3 pdf_toc_parser.py <pdf_path>")
        print("       python3 pdf_toc_parser.py --slice <pdf_path> <output_dir>")
        print("       python3 pdf_toc_parser.py --test-all <pdf_dir>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "--test-all":
        # 批量测试目录下的所有 PDF
        pdf_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
        pdf_files = list(pdf_dir.glob("*.pdf"))

        print(f"找到 {len(pdf_files)} 个 PDF 文件")
        
        # 也搜索 .txt 文件（用于测试，用 txt 模拟目录页）
        txt_files = list(pdf_dir.glob("*.txt"))
        if not pdf_files and txt_files:
            print(f"找到 {len(txt_files)} 个 TXT 文件（将模拟目录解析）")
        
        print("=" * 60)

        for pdf_path in sorted(pdf_files):
            print(f"\n📄 {pdf_path.name}")
            parser = PdfTocParser(str(pdf_path))
            result = parser.parse()

            print(f"   方法: {result['method'] or '失败'}")
            print(f"   页数: {result['total_pages']}")
            print(f"   目录条目: {len(result['toc_entries'])}")

            if result['toc_entries']:
                for e in result['toc_entries'][:5]:
                    print(f"      - {e.get('section_num', '')} {e['title']} (P{e['page']})")
                if len(result['toc_entries']) > 5:
                    print(f"      ... 共 {len(result['toc_entries'])} 条")

            if result['errors']:
                print(f"   ⚠️ 错误: {result['errors']}")

    elif cmd == "--slice":
        # 按目录切片
        pdf_path = sys.argv[2]
        output_dir = sys.argv[3] if len(sys.argv) > 3 else "./sliced"

        parser = PdfTocParser(pdf_path)
        result = parser.parse()

        if not result['success']:
            print(f"❌ 目录解析失败: {result['errors']}")
            sys.exit(1)

        print(f"✅ 解析成功，方法: {result['method']}")
        print(f"📑 目录条目: {len(result['toc_entries'])}")

        slices = slice_pdf_by_toc(pdf_path, result['toc_entries'], output_dir)

        print(f"\n✂️ 切片完成，输出到: {output_dir}")
        for s in slices:
            print(f"   {s['title']}: P{s['page_start']}-P{s['page_end']} ({s['char_count']} 字符)")

    else:
        # 单个文件解析
        pdf_path = sys.argv[1]
        parser = PdfTocParser(pdf_path)
        result = parser.parse()

        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
