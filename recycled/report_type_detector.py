#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
报告类型检测器
区分：年度报告全文 (Annual Report) vs 年度报告摘要 (Summary)
用于在 ELT 流水线入口处过滤掉摘要，避免格式干扰
"""

import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class DetectionResult:
    """检测结果"""
    doc_id: str
    doc_type: str          # "annual_report" | "summary" | "unknown"
    is_summary: bool
    confidence: float      # 0.0 ~ 1.0
    reasons: List[str]
    page_count: Optional[int] = None
    text_preview: str = ""


class ReportTypeDetector:
    """
    A股财报类型检测器
    支持 TXT 和 PDF 两种输入
    """

    # 强特征：标题/封面出现这些词，极大概率是摘要
    SUMMARY_TITLE_KEYWORDS = [
        r"年度报告摘要",
        r"年报摘要",
        r"^\s*摘要\s*$",
    ]

    # 提示语特征：摘要中常见的免责声明/提示
    SUMMARY_HINT_PATTERNS = [
        r"本摘要来自年度报告全文",
        r"为全面了解本公司的经营成果",
        r"年度报告摘要已刊登于",
        r"投资者欲了解详细内容",
        r"应当仔细阅读同时刊载于",
        r"以上.*?摘自.*?年度报告",
        r"本公司董事会保证本摘要",
        r"详见.*年度报告全文",
    ]

    # 摘要特有的章节标题（完整年报一般不会有这些标题）
    SUMMARY_SECTION_PATTERNS = [
        r"(?:^|\n)\s*公司经营情况\s*(?:\n|$)",
        r"(?:^|\n)\s*主要会计数据\s*(?:\n|$)",
        r"(?:^|\n)\s*主要财务指标\s*(?:\n|$)",
        r"(?:^|\n)\s*(?:三|3)\.\d+\s*主要会计数据",
    ]

    # 摘要的页数上限（仅作为辅助判断）
    SUMMARY_PAGE_THRESHOLD = 30

    def __init__(self, skip_log_path: Optional[Path] = None):
        """
        Args:
            skip_log_path: 被跳过的摘要文件记录路径，默认跳过记录
        """
        self.skip_log_path = skip_log_path
        self._skipped_records = []

    # ------------------------------------------------------------------
    # 核心检测逻辑
    # ------------------------------------------------------------------
    def detect_from_text(self, text: str, doc_id: str = "unknown",
                         page_count: Optional[int] = None) -> DetectionResult:
        """
        从纯文本内容判断是否为年度报告摘要

        策略权重：
          - 标题关键词（权重最高）
          - 提示语/免责声明（高权重）
          - 摘要特有章节（中权重）
          - 页数极少 + 含"摘要"字样（低权重）
        """
        reasons = []
        score = 0.0

        # 1) 检查标题区域（前 3000 字符）
        head = text[:3000]

        for pattern in self.SUMMARY_TITLE_KEYWORDS:
            if re.search(pattern, head, re.MULTILINE):
                reasons.append(f"标题区域匹配关键词: {pattern}")
                score += 0.60
                break

        # 2) 检查摘要提示语（全文范围，但优先前半部分）
        search_range = text[:20000] if len(text) > 20000 else text
        for pattern in self.SUMMARY_HINT_PATTERNS:
            if re.search(pattern, search_range, re.IGNORECASE):
                reasons.append(f"发现摘要提示语: {pattern}")
                score += 0.25
                break  # 只计一次

        # 3) 检查摘要特有章节
        for pattern in self.SUMMARY_SECTION_PATTERNS:
            if re.search(pattern, search_range, re.IGNORECASE):
                reasons.append(f"发现摘要特有章节: {pattern}")
                score += 0.10
                break

        # 4) 页数辅助判断
        if page_count is not None and page_count <= self.SUMMARY_PAGE_THRESHOLD:
            if re.search(r"摘要", head):
                reasons.append(f"页数极少({page_count}页)且含'摘要'字样")
                score += 0.15

        # 封顶
        score = min(1.0, score)
        is_summary = score >= 0.50

        doc_type = "summary" if is_summary else "annual_report"
        if score > 0 and score < 0.50:
            doc_type = "unknown"

        preview = head[:200].replace("\n", " ")

        return DetectionResult(
            doc_id=doc_id,
            doc_type=doc_type,
            is_summary=is_summary,
            confidence=round(score, 3),
            reasons=reasons,
            page_count=page_count,
            text_preview=preview
        )

    def detect_from_txt_file(self, txt_path: Path) -> DetectionResult:
        """从 TXT 文件检测"""
        doc_id = txt_path.stem
        text = txt_path.read_text(encoding="utf-8", errors="ignore")
        # TXT 没有真实页数概念，用字符数/1500 估算
        est_pages = max(1, len(text) // 1500)
        return self.detect_from_text(text, doc_id=doc_id, page_count=est_pages)

    def detect_from_pdf_file(self, pdf_path: Path) -> DetectionResult:
        """从 PDF 文件检测（需要 fitz / PyMuPDF）"""
        doc_id = pdf_path.stem
        try:
            import fitz
        except ImportError:
            logger.warning("PyMuPDF (fitz) 未安装，回退到纯文件名/文本检测")
            return self._fallback_detect(pdf_path)

        doc = fitz.open(str(pdf_path))
        page_count = doc.page_count

        # 提取前 5 页文本作为检测输入
        sample_text = ""
        for i in range(min(5, page_count)):
            sample_text += doc[i].get_text()
        doc.close()

        return self.detect_from_text(sample_text, doc_id=doc_id, page_count=page_count)

    def _fallback_detect(self, pdf_path: Path) -> DetectionResult:
        """无 fitz 时的降级检测：仅通过文件名判断"""
        doc_id = pdf_path.stem
        name_lower = pdf_path.name.lower()
        is_summary = "摘要" in name_lower
        return DetectionResult(
            doc_id=doc_id,
            doc_type="summary" if is_summary else "unknown",
            is_summary=is_summary,
            confidence=0.3 if is_summary else 0.0,
            reasons=["文件名含'摘要'"] if is_summary else [],
            page_count=None,
            text_preview=""
        )

    # ------------------------------------------------------------------
    # 跳过记录
    # ------------------------------------------------------------------
    def record_skip(self, result: DetectionResult, source_path: Path):
        """记录被跳过的摘要文件"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "source_path": str(source_path),
            **asdict(result)
        }
        self._skipped_records.append(record)

        if self.skip_log_path:
            self.skip_log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.skip_log_path.exists():
                existing = json.loads(self.skip_log_path.read_text(encoding="utf-8"))
            else:
                existing = []
            existing.append(record)
            self.skip_log_path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        logger.warning(
            "🚫 跳过摘要文件: %s | 置信度=%.2f | 原因=%s",
            source_path.name, result.confidence, "; ".join(result.reasons)
        )

    def get_skipped_records(self) -> List[dict]:
        return self._skipped_records


# ====================================================================
# CLI / 快速测试
# ====================================================================
def _quick_test():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if len(sys.argv) < 2:
        print("用法: python report_type_detector.py <文件路径>")
        sys.exit(1)

    path = Path(sys.argv[1])
    detector = ReportTypeDetector()

    if path.suffix.lower() == ".pdf":
        result = detector.detect_from_pdf_file(path)
    else:
        result = detector.detect_from_txt_file(path)

    print(f"\n文件: {path.name}")
    print(f"类型: {result.doc_type}")
    print(f"是否摘要: {result.is_summary}")
    print(f"置信度: {result.confidence}")
    print(f"原因: {result.reasons}")
    print(f"页数/估算: {result.page_count}")
    print(f"预览: {result.text_preview[:80]}...")


if __name__ == "__main__":
    _quick_test()
