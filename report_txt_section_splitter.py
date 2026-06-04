#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
txt_section_splitter.py

A-share annual report TXT section splitter.

Goal:
- Input:  .txt annual report
- Output: standardized sections/*.txt + standard_sections.json + qa/section_detect_report.json
- No AI / no PDF dependency

Usage:
python report_txt_section_splitter.py 2024_600519.txt --out processed/2024_600519 --doc-id 2024_600519

Design:
- Standard section names are fixed.
- Raw titles are mapped to canonical section names via aliases.
- Detection is based on line-level candidates, directory filtering, order constraint, and confidence scoring.
- Extensible: aliases, heading patterns, filters, and output policy are isolated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = "0.1.0"


STANDARD_SECTIONS = [
    "重要提示_目录_释义",
    "公司简介和主要财务指标",
    "管理层讨论与分析",
    "公司治理",
    "环境和社会责任",
    "重要事项",
    "股份变动及股东情况",
    "债券相关情况",
    "财务报告",
]

SECTION_ALIASES: Dict[str, List[str]] = {
    "重要提示_目录_释义": [
        "重要提示", "目录", "释义", "重要提示、目录和释义", "重要提示、目录及释义",
    ],
    "公司简介和主要财务指标": [
        "公司简介和主要财务指标", "公司简介", "公司基本情况", "主要财务指标",
        "主要会计数据和财务指标", "公司基本情况和主要财务指标",
    ],
    "管理层讨论与分析": [
        "管理层讨论与分析", "经营情况讨论与分析", "董事会报告",
        "报告期内公司所处行业情况", "管理层分析与讨论",
    ],
    "公司治理": [
        "公司治理", "公司治理情况", "治理情况",
    ],
    "环境和社会责任": [
        "环境和社会责任", "环境与社会责任", "社会责任", "环境保护相关情况",
        "社会责任情况", "环境、社会及公司治理",
    ],
    "重要事项": [
        "重要事项", "重大事项", "其他重要事项",
    ],
    "股份变动及股东情况": [
        "股份变动及股东情况", "股份变动和股东情况", "股东和实际控制人情况",
        "股份变动、股东情况", "普通股股份变动及股东情况",
    ],
    "债券相关情况": [
        "债券相关情况", "公司债券相关情况", "可转换公司债券相关情况",
        "优先股相关情况", "债券情况",
    ],
    "财务报告": [
        "财务报告", "审计报告", "财务报表", "财务报表附注", "审计报告及财务报表",
    ],
}

SECTION_INDEX = {name: i for i, name in enumerate(STANDARD_SECTIONS)}


@dataclass
class Candidate:
    canonical_name: str
    raw_title: str
    line_no: int
    start: int
    end: int
    confidence: float
    evidence: List[str] = field(default_factory=list)


@dataclass
class SectionResult:
    section_id: str
    order: int
    canonical_name: str
    raw_title: Optional[str]
    start: Optional[int]
    end: Optional[int]
    start_line: Optional[int]
    end_line: Optional[int]
    char_count: int
    confidence: float
    status: str
    output_file: str
    evidence: List[str] = field(default_factory=list)


class TxtSectionSplitter:
    def __init__(
        self,
        input_path: Path,
        output_dir: Path,
        doc_id: Optional[str] = None,
        create_empty_missing: bool = True,
        min_section_chars: int = 200,
        max_toc_ratio: float = 0.18,
    ) -> None:
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.doc_id = doc_id or self.input_path.stem
        self.create_empty_missing = create_empty_missing
        self.min_section_chars = min_section_chars
        self.max_toc_ratio = max_toc_ratio

    def run(self) -> Dict:
        raw_text = self._read_text(self.input_path)
        text = self._normalize_text(raw_text)
        lines = self._line_index(text)

        candidates = self._detect_candidates(lines, len(text))
        selected = self._select_candidates(candidates)
        sections = self._build_sections(text, lines, selected)
        qa = self._build_qa(text, candidates, selected, sections)

        self._write_outputs(text, sections, candidates, selected, qa)

        return {
            "schema_version": SCHEMA_VERSION,
            "doc_id": self.doc_id,
            "source": {
                "type": "txt",
                "path": str(self.input_path),
                "file_name": self.input_path.name,
            },
            "standard_sections": [asdict(s) for s in sections],
            "qa": qa,
        }

    # ---------- IO ----------

    def _read_text(self, path: Path) -> str:
        encodings = ["utf-8-sig", "utf-8", "gb18030", "gbk", "big5"]
        last_err = None
        for enc in encodings:
            try:
                return path.read_text(encoding=enc)
            except UnicodeDecodeError as e:
                last_err = e
        raise UnicodeDecodeError("unknown", b"", 0, 1, f"cannot decode file: {last_err}")

    def _write_outputs(
        self,
        text: str,
        sections: List[SectionResult],
        candidates: List[Candidate],
        selected: List[Candidate],
        qa: Dict,
    ) -> None:
        sections_dir = self.output_dir / "sections"
        qa_dir = self.output_dir / "qa"
        sections_dir.mkdir(parents=True, exist_ok=True)
        qa_dir.mkdir(parents=True, exist_ok=True)

        for sec in sections:
            out_path = self.output_dir / sec.output_file
            if sec.start is None or sec.end is None:
                content = ""
            else:
                content = text[sec.start:sec.end].strip() + "\n"
            out_path.write_text(content, encoding="utf-8")

        standard_json = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": self.doc_id,
            "source": {
                "type": "txt",
                "path": str(self.input_path),
                "file_name": self.input_path.name,
            },
            "sections": [asdict(s) for s in sections],
        }
        (self.output_dir / "standard_sections.json").write_text(
            json.dumps(standard_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        report = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": self.doc_id,
            "qa": qa,
            "selected_candidates": [asdict(c) for c in selected],
            "all_candidates": [asdict(c) for c in candidates],
        }
        (qa_dir / "section_detect_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- normalize / indexing ----------

    def _normalize_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\u3000", " ")
        text = re.sub(r"[\t ]+", " ", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip() + "\n"

    def _line_index(self, text: str) -> List[Tuple[int, int, str]]:
        """Return list of (start, end, line_text_without_newline)."""
        result = []
        pos = 0
        for line in text.splitlines(keepends=True):
            start = pos
            end = pos + len(line)
            result.append((start, end, line.rstrip("\n")))
            pos = end
        return result

    # ---------- detection ----------

    def _detect_candidates(
        self,
        lines: List[Tuple[int, int, str]],
        total_len: int,
    ) -> List[Candidate]:
        candidates: List[Candidate] = []
        toc_cutoff = int(total_len * self.max_toc_ratio)

        for line_no, (start, end, raw_line) in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue

            if self._is_noise_line(line):
                continue

            if self._looks_like_toc_line(line):
                continue

            canonical, alias = self._match_alias(line)
            if canonical is None:
                continue

            if not self._looks_like_section_heading(line, canonical):
                continue

            evidence = [f"alias:{alias}"]
            confidence = 0.55

            if self._has_section_prefix(line):
                confidence += 0.25
                evidence.append("section_prefix")

            if start > toc_cutoff:
                confidence += 0.10
                evidence.append("outside_toc_region")
            else:
                # 不直接排除，因为有些文本没有目录；只降权。
                confidence -= 0.10
                evidence.append("early_document_region")

            if len(line) <= 40:
                confidence += 0.05
                evidence.append("short_heading")

            confidence = max(0.0, min(1.0, confidence))

            candidates.append(
                Candidate(
                    canonical_name=canonical,
                    raw_title=line,
                    line_no=line_no,
                    start=start,
                    end=end,
                    confidence=round(confidence, 3),
                    evidence=evidence,
                )
            )

        return candidates

    def _match_alias(self, line: str) -> Tuple[Optional[str], Optional[str]]:
        compact = self._compact(line)
        for canonical, aliases in SECTION_ALIASES.items():
            for alias in aliases:
                if self._compact(alias) in compact:
                    return canonical, alias
        return None, None

    def _looks_like_section_heading(self, line: str, canonical: str) -> bool:
        compact = self._compact(line)

        # 太长通常不是标题，可能是正文句子。
        if len(compact) > 70:
            return False

        # 数字密度太高，通常是目录或表格行。
        digit_ratio = sum(ch.isdigit() for ch in compact) / max(1, len(compact))
        if digit_ratio > 0.35:
            return False

        # 常规章节标题。
        if self._has_section_prefix(line):
            return True

        # 无“第X节”的标题也允许，但必须比较短。
        if len(compact) <= 24:
            return True

        return False

    def _has_section_prefix(self, line: str) -> bool:
        s = line.strip()
        patterns = [
            r"^第[一二三四五六七八九十百零〇两]+节[ ：:、\-—]*",
            r"^第\d+节[ ：:、\-—]*",
            r"^第[一二三四五六七八九十百零〇两]+章[ ：:、\-—]*",
            r"^第\d+章[ ：:、\-—]*",
        ]
        return any(re.match(p, s) for p in patterns)

    def _looks_like_toc_line(self, line: str) -> bool:
        s = line.strip()
        # 第三节 管理层讨论与分析 ........ 35
        if re.search(r"[\.·•…]{2,}\s*\d+\s*$", s):
            return True
        # 第三节 管理层讨论与分析 35
        if self._has_section_prefix(s) and re.search(r"\s+\d{1,4}\s*$", s):
            return True
        return False

    def _is_noise_line(self, line: str) -> bool:
        s = line.strip()
        if len(s) <= 1:
            return True
        if re.fullmatch(r"[-_=—·.\s]+", s):
            return True
        if re.fullmatch(r"第?\s*\d+\s*页", s):
            return True
        return False

    def _compact(self, s: str) -> str:
        return re.sub(r"[\s　:：、，,。\.．\-—_/\\（）()\[\]【】]+", "", s)

    # ---------- selection ----------

    def _select_candidates(self, candidates: List[Candidate]) -> List[Candidate]:
        """
        Select at most one candidate per canonical section.
        Primary policy:
        - prefer candidates with section prefix and higher confidence
        - enforce canonical order when possible
        - avoid repeated aliases from directory region
        """
        if not candidates:
            return []

        grouped: Dict[str, List[Candidate]] = {}
        for c in candidates:
            grouped.setdefault(c.canonical_name, []).append(c)

        selected: List[Candidate] = []
        last_order = -1
        last_start = -1

        for canonical in STANDARD_SECTIONS:
            options = grouped.get(canonical, [])
            if not options:
                continue

            # 候选排序：高置信度优先；有章节前缀优先；位置靠后一点优先避免目录。
            options = sorted(
                options,
                key=lambda c: (
                    c.confidence,
                    1 if "section_prefix" in c.evidence else 0,
                    c.start,
                ),
                reverse=True,
            )

            chosen = None
            current_order = SECTION_INDEX[canonical]
            for c in options:
                if current_order < last_order:
                    continue
                if c.start <= last_start:
                    continue
                chosen = c
                break

            if chosen is not None:
                selected.append(chosen)
                last_order = current_order
                last_start = chosen.start

        return sorted(selected, key=lambda c: c.start)

    # ---------- section build ----------

    def _build_sections(
        self,
        text: str,
        lines: List[Tuple[int, int, str]],
        selected: List[Candidate],
    ) -> List[SectionResult]:
        line_no_by_pos = self._line_no_by_start(lines)
        selected_by_name = {c.canonical_name: c for c in selected}
        selected_sorted = sorted(selected, key=lambda c: c.start)

        next_start_by_name: Dict[str, int] = {}
        next_line_by_name: Dict[str, int] = {}
        for i, c in enumerate(selected_sorted):
            next_c = selected_sorted[i + 1] if i + 1 < len(selected_sorted) else None
            next_start_by_name[c.canonical_name] = next_c.start if next_c else len(text)
            next_line_by_name[c.canonical_name] = next_c.line_no if next_c else len(lines)

        results: List[SectionResult] = []

        for idx, canonical in enumerate(STANDARD_SECTIONS, start=1):
            c = selected_by_name.get(canonical)
            file_name = f"sections/{idx:02d}_{canonical}.txt"
            if c is None:
                if not self.create_empty_missing:
                    continue
                results.append(
                    SectionResult(
                        section_id=f"sec_{idx:02d}",
                        order=idx,
                        canonical_name=canonical,
                        raw_title=None,
                        start=None,
                        end=None,
                        start_line=None,
                        end_line=None,
                        char_count=0,
                        confidence=0.0,
                        status="missing",
                        output_file=file_name,
                        evidence=[],
                    )
                )
                continue

            start = c.start
            end = next_start_by_name[canonical]
            content_len = max(0, end - start)
            status = "matched" if content_len >= self.min_section_chars else "too_short"

            results.append(
                SectionResult(
                    section_id=f"sec_{idx:02d}",
                    order=idx,
                    canonical_name=canonical,
                    raw_title=c.raw_title,
                    start=start,
                    end=end,
                    start_line=c.line_no,
                    end_line=next_line_by_name[canonical],
                    char_count=content_len,
                    confidence=c.confidence,
                    status=status,
                    output_file=file_name,
                    evidence=c.evidence,
                )
            )

        return results

    def _line_no_by_start(self, lines: List[Tuple[int, int, str]]) -> Dict[int, int]:
        return {start: i for i, (start, _end, _line) in enumerate(lines, start=1)}

    # ---------- QA ----------

    def _build_qa(
        self,
        text: str,
        candidates: List[Candidate],
        selected: List[Candidate],
        sections: List[SectionResult],
    ) -> Dict:
        warnings: List[str] = []

        matched = [s for s in sections if s.status in {"matched", "too_short"}]
        missing = [s.canonical_name for s in sections if s.status == "missing"]
        too_short = [s.canonical_name for s in sections if s.status == "too_short"]

        if missing:
            warnings.append(f"缺失标准章节: {missing}")
        if too_short:
            warnings.append(f"章节内容过短，可能误切: {too_short}")
        if not selected:
            warnings.append("未识别到任何标准章节")
        if len(text) < 5000:
            warnings.append("文本总长度过短，可能不是完整年报")

        # 顺序检查。
        selected_orders = [SECTION_INDEX[c.canonical_name] for c in selected]
        if selected_orders != sorted(selected_orders):
            warnings.append("章节顺序异常")

        status = "pass"
        if warnings:
            status = "warning"
        if not selected or len(text) < 1000:
            status = "fail"

        return {
            "status": status,
            "warnings": warnings,
            "metrics": {
                "text_chars": len(text),
                "candidate_count": len(candidates),
                "selected_count": len(selected),
                "matched_section_count": len(matched),
                "missing_section_count": len(missing),
            },
            "missing_sections": missing,
            "too_short_sections": too_short,
        }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Split A-share annual report TXT into standardized sections.")
    parser.add_argument("txt", help="input txt file")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--doc-id", default=None, help="document id, default=input file stem")
    parser.add_argument("--no-empty-missing", action="store_true", help="do not create empty files for missing sections")
    parser.add_argument("--min-section-chars", type=int, default=200)
    parser.add_argument("--max-toc-ratio", type=float, default=0.18)

    args = parser.parse_args(argv)

    splitter = TxtSectionSplitter(
        input_path=Path(args.txt),
        output_dir=Path(args.out),
        doc_id=args.doc_id,
        create_empty_missing=not args.no_empty_missing,
        min_section_chars=args.min_section_chars,
        max_toc_ratio=args.max_toc_ratio,
    )
    result = splitter.run()
    print(json.dumps(result["qa"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
