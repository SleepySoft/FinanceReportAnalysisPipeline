#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TXT财报分割器
按照 01.pdf_text_splitter_design.md 设计文档实现

核心思路：
1. 每个 section 只定义"固定文字"标题别名（如 "财务报告"）
2. 由程序自动生成带可选节编号前缀的正则模式
3. 这样无需枚举 "第八节/第九节/第十节..." 等所有节编号组合
"""

import json
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
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
      - 强匹配：带节编号的标题
      - 弱匹配：标题单独出现

    ⚠️ 架构权衡与已知限制（按影响程度排序）：

    1. 弱匹配上限 20 个/section
       超长报告中如果某个标题在正文中出现超过 20 次（如表格反复引用
       "财务报告"），真正的章节标题可能在第 20 次之后，导致遗漏。
       这是为了防止正则回溯导致性能爆炸而做的取舍。

    2. 目录跳过 (ANCHOR_OFFSET=200)
       硬编码的 200 字符偏移量。如果目录排版紧凑，正文中的锚点标题
       可能落在截断范围内被误杀。若锚点本身就在目录中，跳过位置可能
       偏差更大。

    3. 页眉/页脚过滤
       _is_page_header() 匹配行前缀中的 "年度报告"、年份数字等。
       如果真正的章节标题恰好出现在页眉行（如页眉+章节标题在同一行），
       会被误过滤。弱匹配的 0.15~0.25 ratio 阈值也可能误杀短标题。

    4. 子章节合并 (SUBSECTION_MERGE_DISTANCE=800)
       审计报告、关联交易等子章节如果距上一章节 <800 字符，会被
       强制合并到上一章节中。某些报告的审计报告确实很短，会被
       错误吞并。

    5. 负向环视边界
       别名匹配前后不能紧接汉字。如果标题前有标点或特殊符号，
       可能导致匹配失败。

    6. 低质量过滤
       中文比例 <15% 或文件 <500 字节会被直接丢弃。可能误杀
       纯数字表格或极简年报摘要。
    """

    # 章节定义：只写纯标题文字，从长到短排列
    # 同时包含简体、繁体、金融行业非标别名
    SECTIONS = [
        SectionDef("公司简介和主要财务指标", [
            "公司简介和主要财务指标",
            "公司基本情况简介",
            "公司簡介和主要財務指標",
            "公司簡介",
            "公司資料",
            "公司简介",
            "关于我们",
            "關於我們",
        ]),
        SectionDef("管理层讨论与分析", [
            "管理层讨论与分析",
            "管理層討論與分析",
            "管理概览",
            "管理層概覽",
            "经营情况讨论与分析",
            "經營情況討論與分析",
            "经营情况概览",
            "業務回顧",
            "主席致辞",
            "董事長致辭",
        ]),
        SectionDef("公司治理、环境和社会", [
            "公司治理、环境和社会",
            "公司治理环境和社会",
            "公司治理",
            "企業管治",
            "企業管治報告",
            "环境、社会及公司治理",
        ]),
        SectionDef("重要事项", [
            "重要事项",
            "重要事項",
            "董事會報告",
            "董事会报告",
        ]),
        SectionDef("股份变动及股东情况", [
            "股份变动及股东情况",
            "股份变动及股东",
            "股本变动及重要股东情况",
            "股本變動及重要股東情況",
            "股東情況",
            "股东情况",
        ]),
        SectionDef("债券相关情况", [
            "债券相关情况",
            "债券相关",
            "債券相關情況",
            "債券相關",
        ]),
        SectionDef("财务报告", [
            "财务报告",
            "財務報告",
            "財務報表",
            "财务报表",
            "审计报告及财务报告",
            "審計報告及財務報告",
            "獨立核數師報告",
        ]),
        SectionDef("审计报告", [
            "审计报告",
            "審計報告",
            "獨立核數師報告",
        ]),
        SectionDef("关联方及关联交易", [
            "关联方及关联交易",
            "關聯方及關聯交易",
            "關聯方關係及交易",
            "关联方关系及交易",
        ]),
        SectionDef("重要交易和事项", [
            "重要交易和事项",
            "重要交易和事項",
        ]),
        SectionDef("重要提示、目录和释义", [
            "重要提示、目录和释义",
            "重要提示、目錄和釋義",
            "重要提示",
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

    # 通用节编号前缀正则
    # 支持：第X节/章、（X）/（X）、X、/X. 等格式
    SECTION_PREFIX_RE = r'(?:第[一二三四五六七八九十0-9]+(?:节|章)|[（(][一二三四五六七八九十0-9]+[)）]|[一二三四五六七八九十0-9]+[、.．])\s*'

    # 章节编号映射（个位预留，间隔为10）
    SECTION_ORDER_MAP = {
        "前言及重要提示": 10,
        "重要提示、目录和释义": 20,
        "公司简介和主要财务指标": 30,
        "管理层讨论与分析": 40,
        "公司治理、环境和社会": 50,
        "重要事项": 60,
        "股份变动及股东情况": 70,
        "债券相关情况": 80,
        "财务报告": 90,
        "审计报告": 100,
        "关联方及关联交易": 110,
        "重要交易和事项": 120,
    }

    def __init__(self):
        self._compile_patterns()
        self._header_re = re.compile(r'年度报告|半年度报告|季度报告|股份有限公司|\d{4}年(?:度)?(?:报|报告)|季报|半年报')

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
        1) 强匹配：带节编号的标题
        2) 弱匹配：标题单独出现
        均附加负向环视，防止匹配到更长词内部的子串。

        ⚠️ 性能与精度权衡：

        - 负向环视 (?<![\u4e00-\u9fa5]) 和 (?![\u4e00-\u9fa5]) 要求匹配
          边界前后不能是汉字。这意味着 "财务报告分析" 中的 "财务报告"
          不会被匹配（正确），但 "、财务报告" 中的 "财务报告" 也不会被
          匹配（因为顿号不是汉字，前面是汉字顿号，后面是汉字——实际上
          顿号不是 \u4e00-\u9fff 范围，所以顿号后的 "财务报告" 前面是顿号
          （非汉字），环视通过；后面是"分"（汉字），(?![\u4e00-\u9fa5]) 会
          在 "报" 后面检查，"分" 是汉字，所以不匹配）。
          等等，让我重新理解... 实际上 "财务报告分析" 中，匹配 "财务报告"
          后，(?![\u4e00-\u9fa5]) 检查 "报" 后面的字符，是 "分"（汉字），
          所以不匹配。这是正确的。
          但如果标题后面跟着标点（如 "财务报告。"），"报" 后面是 "。"，
          不是汉字，所以可以匹配。这也是正确的。

        - 每个别名生成 2 个正则（强+弱），繁体别名进一步增加模式数量。
          对于 10 个 section × 平均 6 个别名 × 2 = 120 个正则，
          在超长文本上同时执行 finditer 有性能风险。当前通过弱匹配
          上限 20 来缓解，但强匹配仍全部执行。

        - SECTION_PREFIX_RE 支持第X节/章、（X）、X、/X. 等格式，但
          无法覆盖所有非标编号（如 "Part 3"、"Section III" 等英文编号）。
          这也是 030 覆盖率停留在 96.8% 的原因之一。
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

        # 1b) 低质量检测
        is_lq, lq_reason = self._is_low_quality(text)
        if is_lq:
            return SplitResult(
                doc_id=doc_id,
                status="ignored_low_quality",
                metadata={"reason": lq_reason}
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

    def split_file(self, file_path: Path, output_dir: Optional[Path] = None,
                   structured: bool = False) -> SplitResult:
        """从文件读取并分割

        Args:
            file_path: 输入文件路径
            output_dir: 输出目录
            structured: 如果为True，按 {output_dir}/{doc_id}/{section}.txt 结构输出
        """
        text = file_path.read_text(encoding='utf-8')
        doc_id = file_path.stem
        result = self.split(text, doc_id=doc_id)

        if output_dir:
            output_dir = Path(output_dir)
            if structured:
                # 结构化输出：每个报告一个子目录
                doc_dir = output_dir / doc_id
                doc_dir.mkdir(parents=True, exist_ok=True)
                for sec_name, sec_text in result.sections.items():
                    order = self.SECTION_ORDER_MAP.get(sec_name, 999)
                    safe_name = sec_name.replace('、', '_').replace('/', '_')
                    out_path = doc_dir / f"{order:03d}_{safe_name}.txt"
                    out_path.write_text(sec_text, encoding='utf-8')

                meta_path = doc_dir / "_metadata.json"
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
            else:
                # 扁平输出：所有文件放在同一目录
                output_dir.mkdir(parents=True, exist_ok=True)
                for sec_name, sec_text in result.sections.items():
                    order = self.SECTION_ORDER_MAP.get(sec_name, 999)
                    safe_name = sec_name.replace('、', '_').replace('/', '_')
                    out_path = output_dir / f"{doc_id}_{order:03d}_{safe_name}.txt"
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

    def _is_low_quality(self, text: str) -> Tuple[bool, str]:
        """检测是否为低质量提取文本

        ⚠️ 误杀风险：

        1) 500 字节阈值
           - 某些极简年报摘要或季度报告可能确实只有几百字，会被误判为
             低质量。但当前输入是 A 股年度报告，正常长度应在数万字以上，
             500 字节作为 safety guard 基本合理。

        2) (cid:) 比例 >5%
           - PDF 提取失败时常见此标记。阈值 5% 是经验值，如果报告含大量
             化学式（如医药股）或特殊符号，可能接近此阈值，但通常不会超标。

        3) 中文比例 <15%
           - 可能误杀以数字表格为主的报告（如金融股的资产负债表纯文本版）。
           - 也可能误杀纯英文的 B 股报告或外资公司年报。
           - 当前输入是 A 股年报，默认以中文为主，15% 是较宽松的阈值。

        Returns:
            (is_low_quality, reason)
        """
        if len(text) == 0:
            return True, "空文件（0字节）"
        if len(text) < 500:
            return True, f"文件过小（{len(text)}字节）"
        # 检测 PDF 提取失败的 (cid:xxx) 标记：按比例而非绝对数量
        cid_count = text.count('(cid:')
        if cid_count > 0:
            cid_ratio = cid_count / len(text)
            if cid_ratio > 0.05:  # 超过5%的字符是(cid:)标记
                return True, f"PDF提取失败标记过多（{cid_ratio:.1%}）"
        # 检测中文比例极低（可能是严重乱码）
        chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        total = len(text.replace(' ', '').replace('\n', ''))
        if total > 0 and chinese / total < 0.15:
            return True, f"中文比例过低（{chinese/total:.1%}），疑似严重乱码"
        return False, ""

    def _find_all_matches(self, text: str) -> List[MatchRecord]:
        """在全文查找所有候选匹配

        策略：
        1. 强匹配（带章节前缀）保留所有通过过滤的匹配
        2. 弱匹配（无前缀）也保留，但最多 20 个/section，避免性能问题
        3. 目录中的匹配会在 _skip_toc 中被过滤，正文中的匹配仍有机会被保留
        4. _dedup_and_sort 最终保留每个 section 的第一个匹配

        ⚠️ 弱匹配上限 20 的 trade-off：
        - 超长报告（如银行、保险年报可达数百万字）中，"财务报告"、"公司治理"
          等词汇可能在正文表格中反复出现。如果真正的章节标题出现在第 20 次
          匹配之后（即前面有 20 次正文引用通过了 _is_likely_heading 过滤），
          该章节将被完全遗漏。
        - 2026-06-09 实测：5118 份报告中 030 覆盖率 96.8%，其中部分缺失
          可能与此上限有关。如需提升覆盖率，可考虑按 section 差异化设置上限
          （如 030 可放宽到 50，100/110 保持 20）。
        - 另外注意：finditer 在遇到 catastrophic backtracking 时可能极慢，
          这也是设置上限的原因之一。繁体别名增加了模式数量，进一步加剧风险。
        """
        records = []

        for sec in self.SECTIONS:
            # 第一阶段：强匹配
            for alias_tag, pattern in self._patterns[sec.canonical_name]:
                if "[弱]" in alias_tag:
                    continue
                for m in pattern.finditer(text):
                    start, end = m.start(), m.end()
                    line_beg = text.rfind('\n', 0, start) + 1
                    line_end = text.find('\n', start)
                    if line_end == -1:
                        line_end = len(text)
                    line_text = text[line_beg:line_end]

                    if not self._is_likely_heading(line_text, start - line_beg, end - line_beg):
                        continue

                    clean_alias = alias_tag.replace("[强]", "")
                    records.append(MatchRecord(
                        start=start,
                        end=end,
                        canonical_name=sec.canonical_name,
                        matched_alias=clean_alias,
                        line_text=line_text.strip()
                    ))

            # 第二阶段：弱匹配（不受强匹配结果影响，始终执行）
            weak_count = 0
            for alias_tag, pattern in self._patterns[sec.canonical_name]:
                if "[强]" in alias_tag:
                    continue
                for m in pattern.finditer(text):
                    start, end = m.start(), m.end()
                    line_beg = text.rfind('\n', 0, start) + 1
                    line_end = text.find('\n', start)
                    if line_end == -1:
                        line_end = len(text)
                    line_text = text[line_beg:line_end]

                    if not self._is_likely_heading(line_text, start - line_beg, end - line_beg):
                        continue

                    clean_alias = alias_tag.replace("[弱]", "")
                    records.append(MatchRecord(
                        start=start,
                        end=end,
                        canonical_name=sec.canonical_name,
                        matched_alias=clean_alias,
                        line_text=line_text.strip()
                    ))
                    weak_count += 1
                    if weak_count >= 20:
                        break
                if weak_count >= 20:
                    break

        return records

    def _is_page_header(self, text: str) -> bool:
        """检测文本是否主要是页眉/页脚内容

        ⚠️ 负面影响：
        - 如果章节标题和页眉恰好位于同一行（如 "XX股份 2025年度报告  第三节 财务报告"），
          该行会被整体判定为页眉，导致真正的章节标题被过滤。
        - 行首包含 "2025" 且长度>5 即判定为页眉，可能误杀以年份开头的章节引言。
        - 对 "年度报告" 的匹配过于宽泛，如果公司名称本身含 "年报" 字样可能误触发。

        当前实现是保守策略：宁可误杀页眉，也不保留大量重复页眉导致的假匹配。
        如果 030/040 等核心章节覆盖率下降，优先检查此处是否过度过滤。
        """
        return bool(self._header_re.search(text))

    def _is_likely_heading(self, line: str, match_start: int, match_end: int) -> bool:
        """判断一行是否可能是真正的章节标题（而非目录行或正文引用）

        ⚠️ 本函数是多层过滤的集合，每层都有误杀风险：

        1) 目录线过滤（dot_like > 5）
           如果标题后面恰好有很多省略号或点号（如旧版 PDF 提取的目录残留），
           真正的标题可能被误判为目录行。

        2) 页码过滤（after 是纯数字 / \\s{5,}\\d+$）
           如果章节标题后面恰好有页码批注，会被过滤。

        3) 引用检测（"详见/参见/请参阅"）
           如果正文中引用章节时格式为 "详见第三节 财务报告"，
           这里的 "财务报告" 会被正确过滤。但如果章节标题本身包含
           "参见" 等字样（如 "参见事项"），也会被误杀。

        4) 引号检测（before 中有引号）
           如果章节标题前面有引号（如 "管理层讨论与分析"），
           这通常是正文引用，但某些报告的标题格式确实带引号，会误杀。

        5) 弱匹配 ratio 阈值（match_len / line_len < 0.15~0.25）
           当一行包含大量其他内容（如页眉+标题+页码），匹配文本占整行
           比例过低时会被过滤。如果标题很短且行很长，可能被误杀。
           threshold 在页眉行前放宽到 0.1，正常行是 0.25。

        6) before 中的汉字长度过滤（<=20 且含 3+ 汉字）
           如果标题前面有较长中文说明（如 "本章为"），会被过滤。
           这可能误杀 "本节 财务报告" 这类非标格式。
        """
        # 1. 目录线过滤（允许页眉+标题的组合）
        # 计算匹配文本后面的点号数量，如果点号集中在后面可能是目录行
        after_text = line[match_end:]
        dot_like = after_text.count('.') + after_text.count('．') + after_text.count('…') + after_text.count('·')
        if dot_like > 5:
            return False

        # 2. 页码过滤：匹配后面紧跟纯数字
        after = after_text.strip()
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
        has_section_prefix = bool(re.search(r'(?:第[一二三四五六七八九十0-9]+(?:节|章)|[（(][一二三四五六七八九十0-9]+[)）]|[一二三四五六七八九十0-9]+[、.．])', matched_text))

        # 页眉检测
        is_header_before = self._is_page_header(before)

        if not has_section_prefix:
            # 弱匹配需要更严格，但如果 before 是页眉则放宽
            if before and not is_header_before:
                if not re.match(r'^(?:第[一二三四五六七八九十]+(?:节|章)|[（(][一二三四五六七八九十0-9]+[)）]|[一二三四五六七八九十0-9]+[、.．]|\d+\s*[、.．])?$', before):
                    if len(before) <= 20 and re.search(r'[\u4e00-\u9fa5]{3,}', before):
                        return False

            match_len = match_end - match_start
            line_len = len(line.strip())
            if line_len > 0:
                ratio = match_len / line_len
                threshold = 0.1 if is_header_before else 0.25
                if ratio < threshold:
                    return False

        return True

    def _skip_toc(self, matches: List[MatchRecord], text: str) -> List[MatchRecord]:
        """以锚点section跳过目录区域

        ⚠️ 副作用与边界情况：

        1) ANCHOR_OFFSET=200 是经验值，非自适应
           - 如果目录排版非常紧凑（锚点标题后紧跟子目录），200 字符可能
             不足以跳过整个目录区，导致后续目录匹配残留。
           - 如果目录和正文之间只有很少的填充内容，锚点后的正文标题可能
             落在 200 字符内被误杀。

        2) 锚点缺失时的退化
           - 如果 ANCHOR_SECTION 和 FALLBACK_ANCHORS 全部缺失（如某些
             非标结构的港股报告），本函数不做任何过滤，所有匹配保留。
             这可能导致目录匹配大量混入最终结果。

        3) 锚点 reinject 的副作用
           - 锚点本身被重新插入 filtered 列表头部，但其位置仍在目录区。
             _dedup_and_sort 会保留每个 canonical_name 的第一次出现，
             如果目录中锚点被 reinject，而正文中还有同名锚点，
             正文的那个会被去重丢弃——但通常目录中的锚点位置更早，
             所以分割结果会把目录内容算入该章节，可能污染内容。
        """
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
            logger.warning("未找到任何锚点 (%s)，目录跳过可能不准确", "/".join(anchor_names))
            return matches

        cutoff = anchor_pos + self.ANCHOR_OFFSET
        filtered = [m for m in matches if m.start >= cutoff]
        anchor_match = next((m for m in matches if m.canonical_name == used_anchor), None)
        if anchor_match and anchor_match not in filtered:
            filtered.insert(0, anchor_match)

        logger.debug("目录跳过: 锚点='%s' 位置=%d, 截断位置=%d, 保留 %d 个匹配",
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
        """合并子section：如果紧跟在父section之后，则不作为独立分隔点

        ⚠️ 硬编码阈值 800 字符的误杀风险：

        - "审计报告" 在某些年报中确实很短（仅几百字的审计意见），
          如果它紧跟在 "财务报告" 之后且距离 <800 字符，会被强制合并
          到财务报告中，导致审计报告章节完全消失。
          实测：100(审计报告) 覆盖率仅 16.6%，部分原因在此。

        - "关联方及关联交易" 如果被放在 "重要事项" 后面且距离很近，
          也会被吞并。

        - 距离计算使用字符数而非语义分析，如果中间有大量空白或
          格式符号，实际内容可能已经很长了但仍被合并。

        如需提升 100/110 的识别率，可缩小 SUBSECTION_MERGE_DISTANCE
        或把这两个 section 从 subsections 集合中移除。
        """
        if not matches:
            return matches

        subsections = {"审计报告", "关联方及关联交易", "重要交易和事项"}
        result = []
        for i, m in enumerate(matches):
            if m.canonical_name in subsections and i > 0:
                prev = matches[i - 1]
                dist = m.start - prev.start
                if dist < self.SUBSECTION_MERGE_DISTANCE:
                    logger.debug("合并子section: '%s' (距 '%s' 仅 %d 字符)",
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
            sections["前言及重要提示"] = pre_content

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
# 批量处理
# ============================================================
def _process_one_file(args: Tuple[Path, Path, bool, bool]) -> Dict:
    """Worker function for parallel processing"""
    file_path, output_dir, structured, quiet = args
    doc_id = file_path.stem
    try:
        splitter = TxtReportSplitter()
        result = splitter.split_file(file_path, output_dir, structured=structured)
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
        if result.status in ("ignored_summary", "ignored_low_quality"):
            detail["reason"] = result.metadata.get("reason", "")
        return detail
    except Exception as e:
        return {
            "doc_id": doc_id,
            "status": "failed",
            "error": str(e),
        }


def batch_split(input_dir: Path, output_dir: Path, pattern: str = "*.txt",
                workers: int = 1, structured: bool = False, quiet: bool = False) -> Dict:
    """批量处理目录下的TXT财报

    Args:
        input_dir: 输入目录
        output_dir: 输出目录
        pattern: 文件匹配模式
        workers: 并行进程数，>1 启用多进程
        structured: 是否按结构化目录输出
        quiet: 安静模式，减少日志输出
    """
    files = sorted(input_dir.glob(pattern))
    total = len(files)

    summary = {
        "total_files": total,
        "success": 0,
        "warning": 0,
        "failed": 0,
        "ignored_summary": 0,
        "elapsed_seconds": 0,
        "details": [],
    }

    start_time = time.time()
    logger.info("=" * 60)
    logger.info("批量处理开始: %d 个文件", total)
    logger.info("输出目录: %s", output_dir)
    logger.info("并行进程: %d", workers)
    logger.info("结构化输出: %s", structured)
    logger.info("=" * 60)

    if workers > 1:
        # 多进程模式
        args_list = [(f, output_dir, structured, quiet) for f in files]
        completed = 0
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_one_file, arg): arg[0] for arg in args_list}
            for future in as_completed(futures):
                detail = future.result()
                summary["details"].append(detail)
                summary[detail["status"]] = summary.get(detail["status"], 0) + 1
                completed += 1
                if not quiet and completed % 100 == 0:
                    elapsed = time.time() - start_time
                    speed = completed / elapsed if elapsed > 0 else 0
                    logger.info("进度: %d/%d (%.1f%%) | 速度: %.1f 文件/秒 | 成功:%d 警告:%d 失败:%d 忽略:%d",
                                completed, total, completed / total * 100, speed,
                                summary.get("success", 0),
                                summary.get("warning", 0),
                                summary.get("failed", 0),
                                summary.get("ignored_summary", 0))
    else:
        # 单进程模式
        splitter = TxtReportSplitter()
        for idx, file_path in enumerate(files, 1):
            doc_id = file_path.stem
            if not quiet:
                logger.info("[%d/%d] %s", idx, total, doc_id)

            try:
                result = splitter.split_file(file_path, output_dir, structured=structured)
            except Exception as e:
                logger.exception("处理失败: %s", doc_id)
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

            if not quiet and idx % 100 == 0:
                elapsed = time.time() - start_time
                speed = idx / elapsed if elapsed > 0 else 0
                logger.info("进度: %d/%d (%.1f%%) | 速度: %.1f 文件/秒 | 成功:%d 警告:%d 失败:%d 忽略:%d",
                            idx, total, idx / total * 100, speed,
                            summary.get("success", 0),
                            summary.get("warning", 0),
                            summary.get("failed", 0),
                            summary.get("ignored_summary", 0))

    elapsed = time.time() - start_time
    summary["elapsed_seconds"] = round(elapsed, 2)

    # 按状态排序 details，失败/警告的放在前面便于查看
    summary["details"].sort(key=lambda d: {"failed": 0, "warning": 1, "success": 2, "ignored_summary": 3}.get(d["status"], 4))

    report_path = output_dir / "_batch_report.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    logger.info("=" * 60)
    logger.info("批量处理完成")
    logger.info("耗时: %.2f 秒", elapsed)
    logger.info("总计: %d, 成功: %d, 警告: %d, 失败: %d, 摘要忽略: %d",
                total,
                summary.get("success", 0),
                summary.get("warning", 0),
                summary.get("failed", 0),
                summary.get("ignored_summary", 0))
    logger.info("报告已保存: %s", report_path)
    logger.info("=" * 60)

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
    parser.add_argument("--workers", type=int, default=1,
                        help="并行进程数，>1 启用多进程 (默认: 1)")
    parser.add_argument("--structured", action="store_true",
                        help="按结构化目录输出: {output_dir}/{doc_id}/{section}.txt")
    parser.add_argument("--quiet", action="store_true",
                        help="安静模式，减少日志输出")

    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)
        # 也降低根日志级别
        logging.getLogger().setLevel(logging.WARNING)

    if args.file:
        splitter = TxtReportSplitter()
        result = splitter.split_file(args.file, args.output_dir, structured=args.structured)
        print(json.dumps({
            "doc_id": result.doc_id,
            "status": result.status,
            "sections": list(result.sections.keys()),
            "section_sizes": {k: len(v) for k, v in result.sections.items()},
            "metadata": result.metadata,
        }, ensure_ascii=False, indent=2))
    else:
        batch_split(args.input_dir, args.output_dir, args.pattern,
                    workers=args.workers, structured=args.structured, quiet=args.quiet)
