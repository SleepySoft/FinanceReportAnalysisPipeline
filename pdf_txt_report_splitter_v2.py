#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TXT财报分割器

设计原则：
1. 固定标题别名 + 动态正则生成。
2. 先跑确定流程，建立可信章节定位表。
3. 只补缺，不覆盖。
4. 候选必须能按章节顺序无冲突地插入定位表，否则只记录、不切分。
5. fallback 仅限特定 section，且仍必须通过顺序接纳规则。
6. 最终切分点表按位置升序排列，依次切分。
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
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class SectionDef:
    """章节定义"""
    canonical_name: str
    aliases: List[str]


@dataclass
class MatchRecord:
    """匹配记录"""
    start: int
    end: int
    canonical_name: str
    matched_alias: str
    line_text: str

    # 新增：用于审查和分层
    source: str = "primary"      # primary / fallback
    tier: int = 1                # 1=强匹配，2=弱匹配，3=fallback
    confidence: float = 1.0
    reason: str = ""


@dataclass
class SplitResult:
    """分割结果"""
    doc_id: str
    status: str
    sections: Dict[str, str] = field(default_factory=dict)
    metadata: Dict = field(default_factory=dict)


# ============================================================
# 核心分割器
# ============================================================

class TxtReportSplitter:
    """TXT财报文本分割器"""

    # --------------------------------------------------------
    # 章节定义
    # --------------------------------------------------------

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
        SectionDef("公司基本情况", [
            "公司基本情况",
            "公司基本情況",
            "本公司基本情况",
            "本公司基本情況",
        ]),
        SectionDef("关联方及关联交易", [
            "关联方及关联交易",
            "關聯方及關聯交易",
            "關聯方關係及交易",
            "关联方关系及交易",
            "关联方及其交易",
            "關聯方及其交易",
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

    # --------------------------------------------------------
    # 顺序定位表
    # --------------------------------------------------------
    # 注意：
    # 这里不是严格“节号”，而是章节顺序带。
    # 如果某些章节在不同报告里可能互换，可以放到同一个 order bucket。
    # 例如“财务报告”和“审计报告”有时关系较复杂，这里先给相邻顺序。
    # 如果回归发现互相倒置较多，可以考虑给相同 order。
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
        "公司基本情况": 105,
        "关联方及关联交易": 110,
        "重要交易和事项": 120,
    }

    # 允许 fallback 的 section。
    # fallback 只补缺，不覆盖已有边界。
    FALLBACK_ALLOWED_SECTIONS = {
        "审计报告",
        "公司基本情况",
        "关联方及关联交易",
        "重要交易和事项",
    }

    # fallback 关键词。
    # 这些比主标题更经验化，所以只在主流程缺失时使用。
    FALLBACK_ALIASES = {
        "审计报告": [
            "审计意见",
            "关键审计事项",
            "注册会计师的责任",
            "管理层和治理层对财务报表的责任",
            "獨立核數師報告",
        ],
        "公司基本情况": [
            "公司的基本情况",
            "本公司的基本情况",
            "公司概况",
            "公司概況",
        ],
        "关联方及关联交易": [
            "关联方关系及其交易",
            "关联方关系及交易",
            "关联方及其交易",
            "关联交易",
            "關聯方關係及其交易",
        ],
        "重要交易和事项": [
            "重要承诺事项",
            "或有事项",
            "资产负债表日后事项",
            "其他重要事项",
        ],
    }

    # 锚点section名称，用于跳过目录
    ANCHOR_SECTION = "公司简介和主要财务指标"

    # 备用锚点必须是 canonical_name，不再使用 alias 名称
    FALLBACK_ANCHORS = [
        "管理层讨论与分析",
        "公司治理、环境和社会",
        "重要事项",
        "财务报告",
    ]

    ANCHOR_OFFSET = 200

    # 弱匹配上限
    WEAK_MATCH_LIMIT_PER_SECTION = 20

    # 是否启用老的子章节距离合并。
    # 当前新设计下，默认关闭，避免硬距离规则破坏顺序定位表。
    ENABLE_SUBSECTION_MERGE = False
    SUBSECTION_MERGE_DISTANCE = 800

    # 通用节编号前缀正则
    SECTION_PREFIX_RE = (
        r"(?:第[一二三四五六七八九十0-9]+(?:节|章)|"
        r"[（(][一二三四五六七八九十0-9]+[)）]|"
        r"[一二三四五六七八九十0-9]+[、.．])\s*"
    )

    def __init__(self):
        self._compile_patterns()
        self._compile_fallback_patterns()
        self._header_re = re.compile(
            r"年度报告|半年度报告|季度报告|股份有限公司|"
            r"\d{4}年(?:度)?(?:报|报告)|季报|半年报"
        )

    # ========================================================
    # 编译正则
    # ========================================================

    def _alias_to_regex(self, alias: str) -> str:
        """将固定文字标题转换为可容忍中间空白的正则片段"""
        chars = [c for c in alias if not c.isspace()]
        if not chars:
            return ""
        body = r"".join(re.escape(c) + r"\s*" for c in chars).rstrip(r"\s*")
        return body

    def _compile_patterns(self):
        """预编译主流程正则模式"""
        self._patterns = {}

        for sec in self.SECTIONS:
            patterns = []
            for alias in sec.aliases:
                body = self._alias_to_regex(alias)
                if not body:
                    continue

                boundary_prefix = r"(?<![\u4e00-\u9fa5])"
                boundary_suffix = r"(?![\u4e00-\u9fa5])"

                strong = (
                    boundary_prefix
                    + f"(?:{self.SECTION_PREFIX_RE})"
                    + body
                    + boundary_suffix
                )
                weak = boundary_prefix + body + boundary_suffix

                patterns.append({
                    "alias": alias,
                    "type": "strong",
                    "pattern": re.compile(strong),
                })
                patterns.append({
                    "alias": alias,
                    "type": "weak",
                    "pattern": re.compile(weak),
                })

            self._patterns[sec.canonical_name] = patterns

    def _compile_fallback_patterns(self):
        """预编译 fallback 正则"""
        self._fallback_patterns = {}

        for section_name, aliases in self.FALLBACK_ALIASES.items():
            patterns = []
            for alias in aliases:
                body = self._alias_to_regex(alias)
                if not body:
                    continue

                boundary_prefix = r"(?<![\u4e00-\u9fa5])"
                boundary_suffix = r"(?![\u4e00-\u9fa5])"

                # fallback 也允许有编号，但不强求
                pattern = (
                    boundary_prefix
                    + f"(?:{self.SECTION_PREFIX_RE})?"
                    + body
                    + boundary_suffix
                )

                patterns.append({
                    "alias": alias,
                    "pattern": re.compile(pattern),
                })

            self._fallback_patterns[section_name] = patterns

    # ========================================================
    # 公共接口
    # ========================================================

    def split(self, text: str, doc_id: str = "unknown") -> SplitResult:
        """对单篇财报文本进行分割"""

        # 1. 摘要检测
        if self._is_summary(text):
            return SplitResult(
                doc_id=doc_id,
                status="ignored_summary",
                metadata={"reason": "检测到年度报告摘要，不属于分析范围"},
            )

        # 2. 低质量检测
        is_lq, lq_reason = self._is_low_quality(text)
        if is_lq:
            return SplitResult(
                doc_id=doc_id,
                status="ignored_low_quality",
                metadata={"reason": lq_reason},
            )

        rejected_candidates = []

        # 3. 主流程：强匹配 + 严格弱匹配
        primary_matches = self._find_primary_matches(text)

        # 4. 目录跳过
        filtered_matches = self._skip_toc(primary_matches, text)

        # 5. 顺序接纳：只补缺、不覆盖、顺序不能倒置
        accepted, rejected = self._select_ordered_boundaries(filtered_matches)
        rejected_candidates.extend(rejected)

        # 6. 可选：老的子章节合并，默认关闭
        if self.ENABLE_SUBSECTION_MERGE:
            accepted = self._merge_subsections(accepted)

        # 7. fallback：仅针对缺失项
        missing_before_fallback = self._find_missing_sections(accepted)
        fallback_matches = self._find_fallback_matches(
            text=text,
            missing_sections=missing_before_fallback,
        )

        for m in sorted(fallback_matches, key=lambda x: x.start):
            ok, reason = self._can_accept_boundary(m, accepted)
            if ok:
                accepted.append(m)
                accepted.sort(key=lambda x: x.start)
            else:
                rejected_candidates.append(self._rejected_dict(m, reason))

        # 8. 最终切分点表
        final_matches = sorted(accepted, key=lambda m: m.start)

        # 9. 分割
        sections, split_meta = self._split_by_matches(text, final_matches)

        # 10. 状态与 metadata
        missing_after_fallback = self._find_missing_sections(final_matches)

        meta = {
            "total_chars": len(text),
            "anchor_section": self.ANCHOR_SECTION,
            "raw_matches_found": len(primary_matches),
            "matches_after_toc_skip": len(filtered_matches),
            "final_matches_used": len(final_matches),
            "missing_sections_before_fallback": missing_before_fallback,
            "missing_sections_after_fallback": missing_after_fallback,
            "accepted_boundaries": [
                self._accepted_dict(m) for m in final_matches
            ],
            "rejected_candidates": rejected_candidates,
            "detected_boundaries": [
                {
                    "name": m.canonical_name,
                    "position": m.start,
                    "line_preview": m.line_text[:60],
                }
                for m in final_matches
            ],
            **split_meta,
        }

        status = "success"

        if not final_matches:
            status = "failed"
            meta["error"] = "未检测到任何章节边界"
        else:
            empty_sections = [
                name for name, content in sections.items()
                if len(content.strip()) == 0
            ]
            if empty_sections:
                status = "warning"
                meta["empty_sections"] = empty_sections
                meta["warning"] = f"发现 {len(empty_sections)} 个空块: {empty_sections}"

            # 如果使用了 fallback，状态仍可视为 success，但 metadata 标记出来
            used_fallback = any(m.source == "fallback" for m in final_matches)
            if used_fallback:
                meta["used_fallback"] = True
                meta["fallback_sections"] = [
                    m.canonical_name for m in final_matches
                    if m.source == "fallback"
                ]
            else:
                meta["used_fallback"] = False

        return SplitResult(
            doc_id=doc_id,
            status=status,
            sections=sections,
            metadata=meta,
        )

    def split_file(
        self,
        file_path: Path,
        output_dir: Optional[Path] = None,
        structured: bool = False,
    ) -> SplitResult:
        """从文件读取并分割"""
        text = file_path.read_text(encoding="utf-8")
        doc_id = file_path.stem
        result = self.split(text, doc_id=doc_id)

        if output_dir:
            output_dir = Path(output_dir)
            if structured:
                doc_dir = output_dir / doc_id
                doc_dir.mkdir(parents=True, exist_ok=True)

                for sec_name, sec_text in result.sections.items():
                    order = self.SECTION_ORDER_MAP.get(sec_name, 999)
                    safe_name = sec_name.replace("、", "_").replace("/", "_")
                    out_path = doc_dir / f"{order:03d}_{safe_name}.txt"
                    out_path.write_text(sec_text, encoding="utf-8")

                meta_path = doc_dir / "_metadata.json"
                meta_path.write_text(
                    json.dumps({
                        "doc_id": result.doc_id,
                        "status": result.status,
                        "metadata": result.metadata,
                        "section_names": list(result.sections.keys()),
                        "section_sizes": {
                            k: len(v) for k, v in result.sections.items()
                        },
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                output_dir.mkdir(parents=True, exist_ok=True)

                for sec_name, sec_text in result.sections.items():
                    order = self.SECTION_ORDER_MAP.get(sec_name, 999)
                    safe_name = sec_name.replace("、", "_").replace("/", "_")
                    out_path = output_dir / f"{doc_id}_{order:03d}_{safe_name}.txt"
                    out_path.write_text(sec_text, encoding="utf-8")

                meta_path = output_dir / f"{doc_id}_metadata.json"
                meta_path.write_text(
                    json.dumps({
                        "doc_id": result.doc_id,
                        "status": result.status,
                        "metadata": result.metadata,
                        "section_names": list(result.sections.keys()),
                        "section_sizes": {
                            k: len(v) for k, v in result.sections.items()
                        },
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        return result

    # ========================================================
    # 基础检测
    # ========================================================

    def _is_summary(self, text: str) -> bool:
        """检测是否为年度报告摘要"""
        first_lines = "\n".join(text.split("\n")[:30])
        return "年度报告摘要" in first_lines

    def _is_low_quality(self, text: str) -> Tuple[bool, str]:
        """检测是否为低质量提取文本"""
        if len(text) == 0:
            return True, "空文件（0字节）"

        if len(text) < 500:
            return True, f"文件过小（{len(text)}字节）"

        cid_count = text.count("(cid:")
        if cid_count > 0:
            cid_ratio = cid_count / len(text)
            if cid_ratio > 0.05:
                return True, f"PDF提取失败标记过多（{cid_ratio:.1%}）"

        chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        total = len(text.replace(" ", "").replace("\n", ""))
        if total > 0 and chinese / total < 0.15:
            return True, f"中文比例过低（{chinese / total:.1%}），疑似严重乱码"

        return False, ""

    # ========================================================
    # 主流程候选生成
    # ========================================================

    def _find_primary_matches(self, text: str) -> List[MatchRecord]:
        """查找主流程候选：强匹配 + 严格弱匹配"""
        records = []

        for sec in self.SECTIONS:
            section_name = sec.canonical_name
            patterns = self._patterns[section_name]

            # 1. 强匹配
            for item in patterns:
                if item["type"] != "strong":
                    continue

                alias = item["alias"]
                pattern = item["pattern"]

                for m in pattern.finditer(text):
                    record = self._make_match_record(
                        text=text,
                        match=m,
                        section_name=section_name,
                        alias=alias,
                        source="primary",
                        tier=1,
                        confidence=0.95,
                        reason="strong_title_with_section_prefix",
                    )

                    if not self._is_likely_heading(
                        record.line_text,
                        record.start - self._line_start_pos(text, record.start),
                        record.end - self._line_start_pos(text, record.start),
                    ):
                        continue

                    records.append(record)

            # 2. 弱匹配
            weak_count = 0
            for item in patterns:
                if item["type"] != "weak":
                    continue

                alias = item["alias"]
                pattern = item["pattern"]

                for m in pattern.finditer(text):
                    record = self._make_match_record(
                        text=text,
                        match=m,
                        section_name=section_name,
                        alias=alias,
                        source="primary",
                        tier=2,
                        confidence=0.75,
                        reason="weak_title_only",
                    )

                    if not self._is_likely_heading(
                        record.line_text,
                        record.start - self._line_start_pos(text, record.start),
                        record.end - self._line_start_pos(text, record.start),
                    ):
                        continue

                    records.append(record)
                    weak_count += 1

                    if weak_count >= self.WEAK_MATCH_LIMIT_PER_SECTION:
                        break

                if weak_count >= self.WEAK_MATCH_LIMIT_PER_SECTION:
                    break

        return records

    def _make_match_record(
        self,
        text: str,
        match: re.Match,
        section_name: str,
        alias: str,
        source: str,
        tier: int,
        confidence: float,
        reason: str,
    ) -> MatchRecord:
        """根据 regex match 构造 MatchRecord"""
        start, end = match.start(), match.end()
        line_beg = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        if line_end == -1:
            line_end = len(text)

        line_text = text[line_beg:line_end]

        return MatchRecord(
            start=start,
            end=end,
            canonical_name=section_name,
            matched_alias=alias,
            line_text=line_text.strip(),
            source=source,
            tier=tier,
            confidence=confidence,
            reason=reason,
        )

    def _line_start_pos(self, text: str, pos: int) -> int:
        """返回 pos 所在行的起始位置"""
        return text.rfind("\n", 0, pos) + 1

    # ========================================================
    # fallback 候选生成
    # ========================================================

    def _find_fallback_matches(
        self,
        text: str,
        missing_sections: List[str],
    ) -> List[MatchRecord]:
        """
        查找 fallback 候选。

        规则：
        1. 只对缺失 section 运行。
        2. 只对 FALLBACK_ALLOWED_SECTIONS 运行。
        3. fallback 只是候选，最终是否采用仍由 _can_accept_boundary 判断。
        """
        records = []

        for section_name in missing_sections:
            if section_name not in self.FALLBACK_ALLOWED_SECTIONS:
                continue

            patterns = self._fallback_patterns.get(section_name, [])
            if not patterns:
                continue

            fallback_count = 0

            for item in patterns:
                alias = item["alias"]
                pattern = item["pattern"]

                for m in pattern.finditer(text):
                    record = self._make_match_record(
                        text=text,
                        match=m,
                        section_name=section_name,
                        alias=alias,
                        source="fallback",
                        tier=3,
                        confidence=0.55,
                        reason="section_specific_fallback",
                    )

                    # fallback 仍然必须像标题行
                    if not self._is_likely_heading(
                        record.line_text,
                        record.start - self._line_start_pos(text, record.start),
                        record.end - self._line_start_pos(text, record.start),
                    ):
                        continue

                    records.append(record)
                    fallback_count += 1

                    # fallback 更保守，避免候选过多
                    if fallback_count >= 10:
                        break

                if fallback_count >= 10:
                    break

        return records

    # ========================================================
    # 标题行判断
    # ========================================================

    def _is_page_header(self, text: str) -> bool:
        """检测文本是否主要是页眉/页脚内容"""
        return bool(self._header_re.search(text))

    def _is_likely_heading(self, line: str, match_start: int, match_end: int) -> bool:
        """判断一行是否可能是真正章节标题"""
        if not line:
            return False

        after_text = line[match_end:]

        # 1. 目录线过滤
        dot_like = (
            after_text.count(".")
            + after_text.count("．")
            + after_text.count("…")
            + after_text.count("·")
        )
        if dot_like > 5:
            return False

        # 2. 页码过滤
        after = after_text.strip()
        if re.match(r"^\d+$", after):
            return False

        # 3. 目录页码格式
        if re.search(r"\s{5,}\d+$", line):
            return False

        # 4. 引用检测
        before = line[:match_start].strip()
        if re.search(r"详见|参见|请参阅|参考|阅读|查阅", before):
            return False

        if re.search(r"[\"“”]", before) and len(before) > 0:
            return False

        # 5. 是否带节编号
        matched_text = line[match_start:match_end]
        has_section_prefix = bool(re.search(
            r"(?:第[一二三四五六七八九十0-9]+(?:节|章)|"
            r"[（(][一二三四五六七八九十0-9]+[)）]|"
            r"[一二三四五六七八九十0-9]+[、.．])",
            matched_text,
        ))

        is_header_before = self._is_page_header(before)

        if not has_section_prefix:
            # 弱匹配更严格
            if before and not is_header_before:
                if not re.match(
                    r"^(?:第[一二三四五六七八九十]+(?:节|章)|"
                    r"[（(][一二三四五六七八九十0-9]+[)）]|"
                    r"[一二三四五六七八九十0-9]+[、.．]|"
                    r"\d+\s*[、.．])?$",
                    before,
                ):
                    if len(before) <= 20 and re.search(r"[\u4e00-\u9fa5]{3,}", before):
                        return False

            match_len = match_end - match_start
            line_len = len(line.strip())
            if line_len > 0:
                ratio = match_len / line_len
                threshold = 0.1 if is_header_before else 0.25
                if ratio < threshold:
                    return False

        return True

    # ========================================================
    # 目录跳过
    # ========================================================

    def _skip_toc(self, matches: List[MatchRecord], text: str) -> List[MatchRecord]:
        """
        以锚点跳过目录区域。

        改动点：
        1. 备用锚点只使用 canonical_name。
        2. 不再强行 reinject 目录中的 anchor，避免污染切分点表。
        3. 如果 cutoff 后仍有正文锚点，自然会保留。
        """
        if not matches:
            return []

        anchor_names = [self.ANCHOR_SECTION] + self.FALLBACK_ANCHORS
        anchor_pos = None
        used_anchor = None

        # 按位置排序后找第一个可用锚点
        sorted_matches = sorted(matches, key=lambda m: m.start)

        for name in anchor_names:
            anchor_matches = [
                m for m in sorted_matches
                if m.canonical_name == name
            ]
            if anchor_matches:
                anchor_pos = anchor_matches[0].start
                used_anchor = name
                break

        if anchor_pos is None:
            logger.warning(
                "未找到任何锚点 (%s)，目录跳过可能不准确",
                "/".join(anchor_names),
            )
            return matches

        cutoff = anchor_pos + self.ANCHOR_OFFSET
        filtered = [m for m in matches if m.start >= cutoff]

        logger.debug(
            "目录跳过: 锚点='%s' 位置=%d, 截断位置=%d, 保留 %d 个匹配",
            used_anchor,
            anchor_pos,
            cutoff,
            len(filtered),
        )

        return filtered

    # ========================================================
    # 顺序接纳逻辑
    # ========================================================

    def _select_ordered_boundaries(
        self,
        matches: List[MatchRecord],
    ) -> Tuple[List[MatchRecord], List[Dict]]:
        """
        从候选中选择最终切分点。

        规则：
        1. 按位置从前到后扫描。
        2. 同一 section 只接受一次。
        3. 加入候选后，章节顺序不能倒置。
        4. 不符合的候选只记录，不切分。
        """
        accepted: List[MatchRecord] = []
        rejected: List[Dict] = []

        # 同一位置可能命中多个 section。
        # 排序时优先级：
        # 1. 位置靠前
        # 2. tier 小的优先，即强匹配优先
        # 3. alias 长的优先
        matches = sorted(
            matches,
            key=lambda m: (m.start, m.tier, -len(m.matched_alias)),
        )

        for m in matches:
            ok, reason = self._can_accept_boundary(m, accepted)
            if ok:
                accepted.append(m)
                accepted.sort(key=lambda x: x.start)
            else:
                rejected.append(self._rejected_dict(m, reason))

        return accepted, rejected

    def _can_accept_boundary(
        self,
        candidate: MatchRecord,
        accepted: List[MatchRecord],
    ) -> Tuple[bool, str]:
        """
        判断候选是否可以进入最终定位表。

        核心原则：
        1. 只补缺，不覆盖。
        2. 插入后章节 order 不能倒置。
        """
        # 1. 只补缺，不覆盖
        if any(m.canonical_name == candidate.canonical_name for m in accepted):
            return False, "duplicate_section_not_overwrite"

        candidate_order = self.SECTION_ORDER_MAP.get(candidate.canonical_name)
        if candidate_order is None:
            return False, "unknown_section_order"

        # 2. 插入后顺序校验
        test = sorted(accepted + [candidate], key=lambda m: m.start)

        prev_order = None
        prev_name = None

        for m in test:
            order = self.SECTION_ORDER_MAP.get(m.canonical_name)
            if order is None:
                return False, f"unknown_section_order:{m.canonical_name}"

            if prev_order is not None and order < prev_order:
                return (
                    False,
                    f"section_order_inversion:{prev_name}({prev_order})"
                    f" -> {m.canonical_name}({order})",
                )

            prev_order = order
            prev_name = m.canonical_name

        return True, "accepted"

    def _find_missing_sections(self, accepted: List[MatchRecord]) -> List[str]:
        """根据最终定位表查找缺失 section"""
        accepted_names = {m.canonical_name for m in accepted}

        missing = []
        for sec in self.SECTIONS:
            if sec.canonical_name not in accepted_names:
                missing.append(sec.canonical_name)

        return missing

    # ========================================================
    # 可选：旧的子章节合并，默认关闭
    # ========================================================

    def _merge_subsections(self, matches: List[MatchRecord]) -> List[MatchRecord]:
        """
        旧逻辑：距离过近的子章节合并。
        当前默认关闭，仅作为可选兼容。
        """
        if not matches:
            return matches

        subsections = {
            "审计报告",
            "关联方及关联交易",
            "重要交易和事项",
        }

        result = []

        for i, m in enumerate(matches):
            if m.canonical_name in subsections and i > 0:
                prev = matches[i - 1]
                dist = m.start - prev.start
                if dist < self.SUBSECTION_MERGE_DISTANCE:
                    logger.debug(
                        "合并子section: '%s' 距 '%s' 仅 %d 字符",
                        m.canonical_name,
                        prev.canonical_name,
                        dist,
                    )
                    continue

            result.append(m)

        return result

    # ========================================================
    # 切分
    # ========================================================

    def _split_by_matches(
        self,
        text: str,
        matches: List[MatchRecord],
    ) -> Tuple[Dict[str, str], Dict]:
        """按匹配位置分割文本"""
        sections = {}

        if not matches:
            return sections, {"section_count": 0}

        matches = sorted(matches, key=lambda m: m.start)

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
            "section_sizes": {
                k: len(v) for k, v in sections.items()
            },
        }

    # ========================================================
    # metadata 辅助
    # ========================================================

    def _accepted_dict(self, m: MatchRecord) -> Dict:
        return {
            "name": m.canonical_name,
            "position": m.start,
            "end": m.end,
            "matched_alias": m.matched_alias,
            "line_preview": m.line_text[:100],
            "source": m.source,
            "tier": m.tier,
            "confidence": m.confidence,
            "reason": m.reason,
            "order": self.SECTION_ORDER_MAP.get(m.canonical_name),
        }

    def _rejected_dict(self, m: MatchRecord, reason: str) -> Dict:
        return {
            "name": m.canonical_name,
            "position": m.start,
            "end": m.end,
            "matched_alias": m.matched_alias,
            "line_preview": m.line_text[:100],
            "source": m.source,
            "tier": m.tier,
            "confidence": m.confidence,
            "reason": m.reason,
            "reject_reason": reason,
            "order": self.SECTION_ORDER_MAP.get(m.canonical_name),
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
        result = splitter.split_file(
            file_path,
            output_dir,
            structured=structured,
        )

        detail = {
            "doc_id": doc_id,
            "status": result.status,
            "sections": list(result.sections.keys()),
            "section_sizes": {
                k: len(v) for k, v in result.sections.items()
            },
            "used_fallback": result.metadata.get("used_fallback", False),
            "final_matches_used": result.metadata.get("final_matches_used", 0),
            "missing_sections_after_fallback": result.metadata.get(
                "missing_sections_after_fallback",
                [],
            ),
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


def batch_split(
    input_dir: Path,
    output_dir: Path,
    pattern: str = "*.txt",
    workers: int = 1,
    structured: bool = False,
    quiet: bool = False,
) -> Dict:
    """批量处理目录下的TXT财报"""
    files = sorted(input_dir.glob(pattern))
    total = len(files)

    summary = {
        "total_files": total,
        "success": 0,
        "warning": 0,
        "failed": 0,
        "ignored_summary": 0,
        "ignored_low_quality": 0,
        "elapsed_seconds": 0,
        "details": [],
    }

    start_time = time.time()

    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("批量处理开始: %d 个文件", total)
    logger.info("输出目录: %s", output_dir)
    logger.info("并行进程: %d", workers)
    logger.info("结构化输出: %s", structured)
    logger.info("=" * 60)

    if workers > 1:
        args_list = [
            (f, output_dir, structured, quiet)
            for f in files
        ]

        completed = 0

        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_process_one_file, arg): arg[0]
                for arg in args_list
            }

            for future in as_completed(futures):
                detail = future.result()
                summary["details"].append(detail)
                summary[detail["status"]] = summary.get(detail["status"], 0) + 1

                completed += 1

                if not quiet and completed % 100 == 0:
                    elapsed = time.time() - start_time
                    speed = completed / elapsed if elapsed > 0 else 0
                    logger.info(
                        "进度: %d/%d (%.1f%%) | 速度: %.1f 文件/秒 | "
                        "成功:%d 警告:%d 失败:%d 摘要忽略:%d 低质忽略:%d",
                        completed,
                        total,
                        completed / total * 100 if total else 0,
                        speed,
                        summary.get("success", 0),
                        summary.get("warning", 0),
                        summary.get("failed", 0),
                        summary.get("ignored_summary", 0),
                        summary.get("ignored_low_quality", 0),
                    )
    else:
        splitter = TxtReportSplitter()

        for idx, file_path in enumerate(files, 1):
            doc_id = file_path.stem

            if not quiet:
                logger.info("[%d/%d] %s", idx, total, doc_id)

            try:
                result = splitter.split_file(
                    file_path,
                    output_dir,
                    structured=structured,
                )
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
                "section_sizes": {
                    k: len(v) for k, v in result.sections.items()
                },
                "used_fallback": result.metadata.get("used_fallback", False),
                "final_matches_used": result.metadata.get("final_matches_used", 0),
                "missing_sections_after_fallback": result.metadata.get(
                    "missing_sections_after_fallback",
                    [],
                ),
            }

            if result.status == "warning":
                detail["warnings"] = result.metadata.get("warning", "")

            if result.status == "failed":
                detail["error"] = result.metadata.get("error", "")

            if result.status in ("ignored_summary", "ignored_low_quality"):
                detail["reason"] = result.metadata.get("reason", "")

            summary["details"].append(detail)

            if not quiet and idx % 100 == 0:
                elapsed = time.time() - start_time
                speed = idx / elapsed if elapsed > 0 else 0
                logger.info(
                    "进度: %d/%d (%.1f%%) | 速度: %.1f 文件/秒 | "
                    "成功:%d 警告:%d 失败:%d 摘要忽略:%d 低质忽略:%d",
                    idx,
                    total,
                    idx / total * 100 if total else 0,
                    speed,
                    summary.get("success", 0),
                    summary.get("warning", 0),
                    summary.get("failed", 0),
                    summary.get("ignored_summary", 0),
                    summary.get("ignored_low_quality", 0),
                )

    elapsed = time.time() - start_time
    summary["elapsed_seconds"] = round(elapsed, 2)

    summary["details"].sort(
        key=lambda d: {
            "failed": 0,
            "warning": 1,
            "success": 2,
            "ignored_summary": 3,
            "ignored_low_quality": 4,
        }.get(d["status"], 5)
    )

    report_path = output_dir / "_batch_report.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("=" * 60)
    logger.info("批量处理完成")
    logger.info("耗时: %.2f 秒", elapsed)
    logger.info(
        "总计: %d, 成功: %d, 警告: %d, 失败: %d, 摘要忽略: %d, 低质忽略: %d",
        total,
        summary.get("success", 0),
        summary.get("warning", 0),
        summary.get("failed", 0),
        summary.get("ignored_summary", 0),
        summary.get("ignored_low_quality", 0),
    )
    logger.info("报告已保存: %s", report_path)
    logger.info("=" * 60)

    return summary


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TXT财报分割器")

    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("samples"),
        help="输入目录，默认: samples",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/split"),
        help="输出目录，默认: output/split",
    )

    parser.add_argument(
        "--pattern",
        default="*.txt",
        help="文件匹配模式，默认: *.txt",
    )

    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="单文件处理模式",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并行进程数，>1 启用多进程，默认: 1",
    )

    parser.add_argument(
        "--structured",
        action="store_true",
        help="按结构化目录输出: {output_dir}/{doc_id}/{section}.txt",
    )

    parser.add_argument(
        "--quiet",
        action="store_true",
        help="安静模式，减少日志输出",
    )

    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)
        logging.getLogger().setLevel(logging.WARNING)

    if args.file:
        splitter = TxtReportSplitter()
        result = splitter.split_file(
            args.file,
            args.output_dir,
            structured=args.structured,
        )

        print(json.dumps({
            "doc_id": result.doc_id,
            "status": result.status,
            "sections": list(result.sections.keys()),
            "section_sizes": {
                k: len(v) for k, v in result.sections.items()
            },
            "metadata": result.metadata,
        }, ensure_ascii=False, indent=2))
    else:
        batch_split(
            args.input_dir,
            args.output_dir,
            args.pattern,
            workers=args.workers,
            structured=args.structured,
            quiet=args.quiet,
        )
