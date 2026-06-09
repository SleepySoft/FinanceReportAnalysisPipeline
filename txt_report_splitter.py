#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TXT财报分割器
按照 01.pdf_text_splitter_design.md 设计文档实现

核心思路：
1. 每个 section 只定义“固定文字”标题别名（如 "财务报告"）
2. 由程序自动生成带可选节编号前缀的正则模式：
   (?:第[一二三四五六七八九十0-9]+节\s*)?财\s*务\s*报\s*告
3. 这样无需枚举 "第八节/第九节/第十节..." 等所有节编号组合
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================
@dataclass
class SectionDef:
    """章节定义"""
    canonical_name: str
    aliases: List[str]  # 从长到短排列，优先匹配长的


@dataclass
class MatchRecord:
    """匹配记录"""
    start: int          # 字符起始位置
    end: int            # 字符结束位置
    canonical_name: str
    matched_alias: str
    line_text: str      # 所在行的文本


@dataclass
class SplitResult:
    """分割结果"""
    doc_id: str
    status: str                     # success / warning / failed / ignored_summary
    sections: Dict[str, str] = field(default_factory=dict)
    metadata: Dict = field(default_factory=dict)


# ============================================================
# 核心分割器
# ============================================================
class TxtReportSplitter:
    """TXT财报文本分割器

    别名定义规则：只写标题文字本身，不写 "第X节" 前缀。
    程序会自动为每个标题生成两套模式：
      - 强匹配：(?:第[一二三四五六七八九十0-9]+节\s*)标题
      - 弱匹配：标题（单独出现）
    """

    # 章节定义：只写纯标题文字，从长到短排列
    SECTIONS = [
        SectionDef("公司简介和主要财务指标", [
            "公司简介和主要财务指标",
            "公司简介",
        ]),
        SectionDef("管理层讨论与分析", [
            "管理层讨论与分析",
        ]),
        SectionDef("公司治理、环境和社会", [
            "公司治理、环境和社会",
            "公司治理环境和社会",
            "公司治理",
        ]),
        SectionDef("重要事项", [
            "重要事项",
        ]),
        SectionDef("股份变动及股东情况", [
            "股份变动及股东情况",
            "股份变动及股东",
        ]),
        SectionDef("债券相关情况", [
            "债券相关情况",
            "债券相关",
        ]),
        SectionDef("财务报告", [
            "财务报告",
        ]),
        SectionDef("审计报告", [
            "审计报告",
        ]),
        SectionDef("关联方及关联交易", [
            "关联方及关联交易",
        ]),
        SectionDef("重要交易和事项", [
            "重要交易和事项",
        ]),
        SectionDef("重要提示、目录和释义", [
            "重要提示、目录和释义",
        ]),
    ]

    # 锚点section名称（用于跳过目录）
    ANCHOR_SECTION = "公司简介和主要财务指标"
    # 当主锚点缺失时，尝试的备用锚点（按优先级排序）
    FALLBACK_ANCHORS = ["公司基本情况", "管理层讨论与分析"]
    # 锚点之后的偏移量（字符数），用于进一步跳过目录残留
    ANCHOR_OFFSET = 200
    # 子section合并距离阈值
    SUBSECTION_MERGE_DISTANCE = 800

    # 通用节编号前缀正则（中文数字或阿拉伯数字）
    SECTION_PREFIX_RE = r'第[一二三四五六七八九十0-9]+节\s*'

    def __init__(self):
        self._compile_patterns()

    def _alias_to_regex(self, alias: str) -> str:
        """将固定文字标题转换为可容忍中间空格的正则片段"""
        chars = [c for c in alias if not c.isspace()]
        if not chars:
            return ""
        body = r''.join(re.escape(c) + r'\s*' for c in chars).rstrip(r'\s*')
        return body

    def _compile_patterns(self):
        """预编译正则模式

        对每个别名生成两种模式：
        1) 强匹配：(?:第...节\s*)标题  — 带节编号
        2) 弱匹配：标题                — 单独出现
        均附加负向环视，防止匹配到更长词内部的子串。
        """
        self._patterns = {}
        for sec in self.SECTIONS:
            patterns = []
            for alias in sec.aliases:
                body = self._alias_to_regex(alias)
                if not body:
                    continue
                # 负向环视：前后不能紧接汉字
                boundary_prefix = r'(?<![\u4e00-\u9fa5])'
                boundary_suffix = r'(?![\u4e00-\u9fa5])'

                # 强匹配：必须带节编号
                strong = boundary_prefix + f'(?:{self.SECTION_PREFIX_RE})' + body + boundary_suffix
                patterns.append((alias + "[强]", re.compile(strong)))

                # 弱匹配：单独标题，不带节编号
                weak = boundary_prefix + body + boundary_suffix
                patterns.append((alias + "[弱]", re.compile(weak)))

            self._patterns[sec.canonical_name] = patterns

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def split(self, text: str, doc_id: str = "unknown") -> SplitResult:
        """对单篇财报文本进行分割"""

        # 1) 摘要检测
        if self._is_summary(text):
            return SplitResult(
                doc_id=doc_id,
                status="ignored_summary",
                metadata={"reason": "检测到年度报告摘要，不属于分析范围"}
            )

        # 2) 找到所有候选匹配
        all_matches = self._find_all_matches(text)

        # 3) 以锚点跳过目录
        filtered = self._skip_toc(all_matches, text)

        # 4) 去重 + 排序 + 合并子section
        final_matches = self._dedup_and_sort(filtered)
        final_matches = self._merge_subsections(final_matches)

        # 5) 分割文本
        sections, split_meta = self._split_by_matches(text, final_matches)

        # 6) 组装 metadata
        meta = {
            "total_chars": len(text),
            "anchor_section": self.ANCHOR_SECTION,
            "raw_matches_found": len(all_matches),
            "matches_after_toc_skip": len(filtered),
            "final_matches_used": len(final_matches),
            "detected_boundaries": [
                {"name": m.canonical_name, "position": m.start, "line_preview": m.line_text[:60]}
                for m in final_matches
            ],
            **split_meta,
        }

        # 7) 状态判定
        status = "success"
        empty_sections = [name for name, content in sections.items() if len(content.strip()) == 0]
        if empty_sections:
            status = "warning"
            meta["empty_sections"] = empty_sections
            meta["warning"] = f"发现 {len(empty_sections)} 个空块: {empty_sections}"

        if len(final_matches) == 0:
            status = "failed"
            meta["error"] = "未检测到任何章节边界"

        return SplitResult(
            doc_id=doc_id,
            status=status,
            sections=sections,
            metadata=meta
        )

    def split_file(self, file_path: Path, output_dir: Optional[Path] = None) -> SplitResult:
        """从文件读取并分割"""
        text = file_path.read_text(encoding='utf-8')
        doc_id = file_path.stem
        result = self.split(text, doc_id=doc_id)

        if output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            for sec_name, sec_text in result.sections.items():
                safe_name = sec_name.replace('、', '_').replace('/', '_')
                out_path = output_dir / f"{doc_id}_{safe_name}.txt"
                out_path.write_text(sec_text, encoding='utf-8')

            meta_path = output_dir / f"{doc_id}_metadata.json"
            meta_path.write_text(
                json.dumps({
                    "doc_id": result.doc_id,
                    "status": result.status,
                    "metadata": result.metadata,
                    "section_names": list(result.sections.keys()),
                    "section_sizes": {k: len(v) for k, v in result.sections.items()},
                }, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )

        return result

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _is_summary(self, text: str) -> bool:
        """检测是否为年度报告摘要"""
        first_lines = '\n'.join(text.split('\n')[:30])
        return "年度报告摘要" in first_lines

    def _find_all_matches(self, text: str) -> List[MatchRecord]:
        """在全文查找所有候选匹配"""
        records = []

        for sec in self.SECTIONS:
            sec_matched = False
            for alias_tag, pattern in self._patterns[sec.canonical_name]:
                if sec_matched:
                    break
                for m in pattern.finditer(text):
                    start, end = m.start(), m.end()
                    # 提取所在行
                    line_beg = text.rfind('\n', 0, start) + 1
                    line_end = text.find('\n', start)
                    if line_end == -1:
                        line_end = len(text)
                    line_text = text[line_beg:line_end]

                    if not self._is_likely_heading(line_text, start - line_beg, end - line_beg):
                        continue

                    # 记录原始别名（去掉 [强]/[弱] 标记）
                    clean_alias = alias_tag.replace("[强]", "").replace("[弱]", "")
                    records.append(MatchRecord(
                        start=start,
                        end=end,
                        canonical_name=sec.canonical_name,
                        matched_alias=clean_alias,
                        line_text=line_text.strip()
                    ))
                    sec_matched = True
                    break

        return records

    def _is_likely_heading(self, line: str, match_start: int, match_end: int) -> bool:
        """判断一行是否可能是真正的章节标题（而非目录行或正文引用）"""
        # 1. 目录线过滤
        dot_like = line.count('.') + line.count('．') + line.count('…') + line.count('·')
        if dot_like > 5:
            return False

        # 2. 页码过滤：匹配后面紧跟纯数字
        after = line[match_end:].strip()
        if re.match(r'^\d+$', after):
            return False

        # 3. 目录页码格式：大量空格后跟数字
        if re.search(r'\s{5,}\d+$', line):
            return False

        # 4. 通用引用检测
        before = line[:match_start].strip()
        if re.search(r'详见|参见|请参阅|参考|阅读|查阅', before):
            return False
        if re.search(r'[\"“”]', before) and len(before) > 0:
            return False

        # 5. 判断实际匹配到的文本是否带节编号
        matched_text = line[match_start:match_end]
        has_section_prefix = bool(re.search(r'第[一二三四五六七八九十0-9]+节', matched_text))

        if not has_section_prefix:
            # 弱匹配需要更严格
            if before:
                if not re.match(r'^(?:第[一二三四五六七八九十]+节|[（(]?[一二三四五六七八九十]+[)）]?\s*[、.．]?|\d+\s*[、.．]?)?$', before):
                    if len(before) <= 20 and re.search(r'[\u4e00-\u9fa5]{3,}', before):
                        return False

            match_len = match_end - match_start
            line_len = len(line.strip())
            if line_len > 0 and match_len / line_len < 0.25:
                return False

        return True

    def _skip_toc(self, matches: List[MatchRecord], text: str) -> List[MatchRecord]:
        """以锚点section跳过目录区域"""
        anchor_names = [self.ANCHOR_SECTION] + self.FALLBACK_ANCHORS
        anchor_pos = None
        used_anchor = None

        for name in anchor_names:
            anchor_matches = [m for m in matches if m.canonical_name == name]
            if anchor_matches:
                anchor_pos = anchor_matches[0].start
                used_anchor = name
                break

        if anchor_pos is None:
            logger.warning("⚠️ 未找到任何锚点 (%s)，目录跳过可能不准确", "/".join(anchor_names))
            return matches

        cutoff = anchor_pos + self.ANCHOR_OFFSET
        filtered = [m for m in matches if m.start >= cutoff]
        anchor_match = next((m for m in matches if m.canonical_name == used_anchor), None)
        if anchor_match and anchor_match not in filtered:
            filtered.insert(0, anchor_match)

        logger.info("📖 目录跳过: 锚点='%s' 位置=%d, 截断位置=%d, 保留 %d 个匹配",
                    used_anchor, anchor_pos, cutoff, len(filtered))
        return filtered

    def _dedup_and_sort(self, matches: List[MatchRecord]) -> List[MatchRecord]:
        """按位置排序，每个canonical_name只保留第一个匹配"""
        matches = sorted(matches, key=lambda m: m.start)
        seen = set()
        result = []
        for m in matches:
            if m.canonical_name not in seen:
                seen.add(m.canonical_name)
                result.append(m)
        return result

    def _merge_subsections(self, matches: List[MatchRecord]) -> List[MatchRecord]:
        """合并子section：如果紧跟在父section之后，则不作为独立分隔点"""
        if not matches:
            return matches

        subsections = {"审计报告", "关联方及关联交易", "重要交易和事项"}
        result = []
        for i, m in enumerate(matches):
            if m.canonical_name in subsections and i > 0:
                prev = matches[i - 1]
                dist = m.start - prev.start
                if dist < self.SUBSECTION_MERGE_DISTANCE:
                    logger.debug("🔄 合并子section: '%s' (距 '%s' 仅 %d 字符)",
                                 m.canonical_name, prev.canonical_name, dist)
                    continue
            result.append(m)
        return result

    def _split_by_matches(self, text: str, matches: List[MatchRecord]) -> Tuple[Dict[str, str], Dict]:
        """按匹配位置分割文本"""
        sections = {}
        if not matches:
            return sections, {"section_count": 0}

        pre_content = text[:matches[0].start]
        if pre_content.strip():
            sections["_PREAMBLE"] = pre_content

        for i, match in enumerate(matches):
            start = match.start
            end = matches[i + 1].start if i + 1 < len(matches) else len(text)
            sec_text = text[start:end]
            sections[match.canonical_name] = sec_text

        return sections, {
            "section_count": len(sections),
            "section_sizes": {k: len(v) for k, v in sections.items()},
        }


# ============================================================
# 批量处理入口
# ============================================================
def batch_split(input_dir: Path, output_dir: Path, pattern: str = "*.txt") -> Dict:
    """批量处理目录下的TXT财报"""
    splitter = TxtReportSplitter()
    files = sorted(input_dir.glob(pattern))

    summary = {
        "total_files": len(files),
        "success": 0,
        "warning": 0,
        "failed": 0,
        "ignored_summary": 0,
        "details": [],
    }

    for file_path in files:
        doc_id = file_path.stem
        logger.info("=" * 60)
        logger.info("📄 处理: %s", doc_id)

        try:
            result = splitter.split_file(file_path, output_dir)
        except Exception as e:
            logger.exception("❌ 处理失败: %s", doc_id)
            summary["failed"] += 1
            summary["details"].append({
                "doc_id": doc_id,
                "status": "failed",
                "error": str(e),
            })
            continue

        summary[result.status] = summary.get(result.status, 0) + 1
        detail = {
            "doc_id": doc_id,
            "status": result.status,
            "sections": list(result.sections.keys()),
            "section_sizes": {k: len(v) for k, v in result.sections.items()},
        }
        if result.status == "warning":
            detail["warnings"] = result.metadata.get("warning", "")
        if result.status == "failed":
            detail["error"] = result.metadata.get("error", "")
        summary["details"].append(detail)

        logger.info("📊 状态: %s", result.status)
        for name, size in detail["section_sizes"].items():
            flag = " 🚨" if size == 0 else ""
            logger.info("   %s: %d 字符%s", name, size, flag)

    report_path = output_dir / "_batch_report.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info("=" * 60)
    logger.info("📋 批量报告已保存: %s", report_path)
    logger.info("总计: %d, 成功: %d, 警告: %d, 失败: %d, 摘要忽略: %d",
                summary["total_files"],
                summary.get("success", 0),
                summary.get("warning", 0),
                summary.get("failed", 0),
                summary.get("ignored_summary", 0))

    return summary


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TXT财报分割器")
    parser.add_argument("--input-dir", type=Path, default=Path("samples"),
                        help="输入目录 (默认: samples)")
    parser.add_argument("--output-dir", type=Path, default=Path("output/split"),
                        help="输出目录 (默认: output/split)")
    parser.add_argument("--pattern", default="*.txt",
                        help="文件匹配模式 (默认: *.txt)")
    parser.add_argument("--file", type=Path, default=None,
                        help="单文件处理模式")

    args = parser.parse_args()

    if args.file:
        splitter = TxtReportSplitter()
        result = splitter.split_file(args.file, args.output_dir)
        print(json.dumps({
            "doc_id": result.doc_id,
            "status": result.status,
            "sections": list(result.sections.keys()),
            "section_sizes": {k: len(v) for k, v in result.sections.items()},
            "metadata": result.metadata,
        }, ensure_ascii=False, indent=2))
    else:
        batch_split(args.input_dir, args.output_dir, args.pattern)
