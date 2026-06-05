#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A-Share Annual Report ELT Pipeline (Stage 2 & Stage 3)
架构：无损平铺 (Lossless Flat) + 价值打标 (Value Tagging)
依赖: pip install ollama pydantic
"""

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Optional

import ollama
from pydantic import BaseModel, ValidationError

# ==========================================
# 0. 全局配置与价值体系
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

VALUE_TIER_MAP = {
    "管理层讨论与分析": "HIGH",
    "财务报告": "HIGH",
    "重要事项": "MEDIUM",
    "公司治理": "MEDIUM",
    "股份变动及股东情况": "MEDIUM",
    "债券相关情况": "MEDIUM",
    "公司简介和主要财务指标": "LOW",
    "重要提示_目录_释义": "LOW",
    "环境和社会责任": "LOW"
}

NOTES_HIGH_RISK_KEYWORDS = ["关联方", "关联交易", "或有事项", "承诺", "日后事项", "风险", "诉讼"]


# Pydantic 模型
class Section(BaseModel):
    order: int
    raw_title: str
    canonical_name: Optional[str]


class ReportTOC(BaseModel):
    sections: List[Section]


@dataclass
class TextBlock:
    seq_id: str  # 支持层级，如 "007" 或 "007.01"
    start_char: int
    end_char: int
    char_count: int
    raw_title: str
    canonical_name: str
    value_tier: str
    file_path: str


# ==========================================
# 阶段 2: L1 粗切 (全局脑图提取与物理切块)
# ==========================================
class L1CoarseSplitter:
    def __init__(self, input_path: Path, output_dir: Path, model_name: str):
        self.input_path = input_path
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.doc_id = self.input_path.stem
        self.model_name = model_name

    def _extract_toc_with_llm(self, text: str) -> List[Section]:
        head_text = text[:8000]
        prompt = f"""
        你是一个专业的金融数据提取 API。提取以下 A 股财报头部文本的目录结构。
        标准名称(canonical_name)请尽可能映射到以下范围，若不在范围请输出 null：
        ["重要提示_目录_释义", "公司简介和主要财务指标", "管理层讨论与分析", "公司治理", "环境和社会责任", "重要事项", "股份变动及股东情况", "债券相关情况", "财务报告"]
        文本：{head_text}
        """
        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{'role': 'user', 'content': prompt}],
                format=ReportTOC.model_json_schema(),
                options={"temperature": 0.0, "num_ctx": 8192}
            )
            parsed_data = ReportTOC.model_validate_json(response['message']['content'])
            valid_sections = [s for s in parsed_data.sections if s.canonical_name]
            return sorted(valid_sections, key=lambda x: x.order)
        except Exception as e:
            logger.error(f"❌ L1 LLM 提取失败: {e}")
            return []

    def _find_anchors_with_lookahead(self, text: str, toc_list: List[Section]) -> List[Dict]:
        anchors = []
        cursor = min(2000, len(text) // 10)
        TOC_GAP_THRESHOLD = 600  # 防伪探测距离：600字符以内必是假目录

        for i, toc in enumerate(toc_list):
            while True:
                pos = text.find(toc.raw_title, cursor)
                if pos == -1:
                    logger.warning(f"⚠️ 正文未找到: {toc.raw_title}")
                    break

                # Look-ahead 前瞻距离探测
                if i + 1 < len(toc_list):
                    next_toc = toc_list[i + 1]
                    next_pos = text.find(next_toc.raw_title, pos + len(toc.raw_title))
                    if next_pos != -1 and (next_pos - pos) < TOC_GAP_THRESHOLD:
                        logger.debug(f"跳过伪锚点(目录区): {toc.raw_title}")
                        cursor = pos + len(toc.raw_title)
                        continue

                anchors.append({
                    "pos": pos,
                    "raw_title": toc.raw_title,
                    "canonical_name": toc.canonical_name
                })
                cursor = pos + len(toc.raw_title)
                break
        return anchors

    def run(self) -> dict:
        text = self.input_path.read_text(encoding="utf-8", errors="ignore")
        toc_list = self._extract_toc_with_llm(text)
        if not toc_list:
            raise RuntimeError("L1 提取目录失败，中断流水线。")

        anchors = self._find_anchors_with_lookahead(text, toc_list)
        blocks_dir = self.output_dir / "blocks"
        blocks_dir.mkdir(exist_ok=True)
        blocks = []

        first_pos = anchors[0]["pos"] if anchors else len(text)
        if first_pos > 0:
            blocks.append(self._make_block(text, "000", 0, first_pos, "封面与目录", "_PREAMBLE", blocks_dir))

        for i, anchor in enumerate(anchors):
            start = anchor["pos"]
            end = anchors[i + 1]["pos"] if i + 1 < len(anchors) else len(text)
            seq_id = f"{i + 1:03d}"
            blocks.append(
                self._make_block(text, seq_id, start, end, anchor["raw_title"], anchor["canonical_name"], blocks_dir))

        self._verify_lossless(text, blocks)

        manifest = {
            "doc_id": self.doc_id,
            "total_chars": len(text),
            "block_count": len(blocks),
            "blocks": [asdict(b) for b in blocks]
        }
        return manifest

    def _make_block(self, text, seq_id, start, end, title, canonical_name, blocks_dir):
        content = text[start:end]
        safe_name = f"{seq_id}_{canonical_name.replace('/', '_')}"
        file_path = blocks_dir / f"{safe_name}.txt"
        file_path.write_text(content, encoding="utf-8", errors="ignore")
        tier = VALUE_TIER_MAP.get(canonical_name, "UNMAPPED")
        return TextBlock(seq_id, start, end, len(content), title, canonical_name, tier, f"blocks/{file_path.name}")

    def _verify_lossless(self, original_text, blocks):
        reconstructed = "".join([original_text[b.start_char:b.end_char] for b in blocks])
        assert reconstructed == original_text, "L1 发生数据丢失！"


# ==========================================
# 阶段 3: L2 财务细切 (正则与先验知识探针)
# ==========================================
class L2FinancialSplitter:
    def __init__(self, output_dir: Path, manifest: dict):
        self.output_dir = output_dir
        self.manifest = manifest
        self.blocks_dir = self.output_dir / "blocks"

    def run(self) -> dict:
        new_blocks = []
        for block in self.manifest["blocks"]:
            if block["canonical_name"] == "财务报告":
                logger.info(f"🎯 启动 L2 深切: {block['file_path']}")
                file_path = self.output_dir / block["file_path"]
                text = file_path.read_text(encoding="utf-8")

                sub_blocks = self._deep_split(text, block["seq_id"], block["start_char"])
                new_blocks.extend(sub_blocks)
                file_path.unlink()  # 删掉 50 万字的原始大文件
            else:
                new_blocks.append(block)

        self.manifest["blocks"] = new_blocks
        self.manifest["block_count"] = len(new_blocks)
        return self.manifest

    def _deep_split(self, text: str, parent_seq: str, parent_global_start: int) -> list:
        anchors = []
        # 升级版正则容错：解决利润表霸占后续内容的 BUG
        hard_patterns = [
            ("审计报告", r"审计报告|审计意见"),
            ("合并资产负债表", r"(合并及母公司|合并)?资产负债表"),
            ("合并利润表", r"(合并及母公司|合并)?利润表"),
            ("合并现金流量表", r"(合并及母公司|合并)?现金流量表"),
            ("合并所有者权益变动表", r"(合并及母公司|合并|合并及母公司股东|合并股东)?(所有者|股东)权益变动表"),
            ("财务报表附注", r"(财务报表|合并财务报表|会计报表)附注")
        ]

        cursor = 0
        for title, pattern in hard_patterns:
            match = re.search(pattern, text[cursor:])
            if match:
                pos = cursor + match.start()
                anchors.append({"pos": pos, "title": title, "type": "main"})
                cursor = pos + len(match.group())

        # 探测附注内部高危节点
        notes_start = next((a["pos"] for a in anchors if a["title"] == "财务报表附注"), len(text))
        notes_pattern = re.compile(
            r'\n([一二三四五六七八九十十一十二十三十四十五十六十七十八十九二十]+、\s*([^。\n]{2,30}))(?=\n|$)')

        for match in notes_pattern.finditer(text, notes_start):
            anchors.append({
                "pos": match.start() + 1,
                "title": match.group(1).strip(),
                "clean_name": match.group(2).strip(),
                "type": "note_sub"
            })

        anchors = sorted(anchors, key=lambda x: x["pos"])
        sub_blocks = []
        first_pos = anchors[0]["pos"] if anchors else len(text)

        if first_pos > 0:
            sub_blocks.append(
                self._make_sub_block(text, 0, first_pos, parent_seq, 0, parent_global_start, "财务报告_前言",
                                     "财务报告_前言", "MEDIUM"))

        for i, anchor in enumerate(anchors):
            start = anchor["pos"]
            end = anchors[i + 1]["pos"] if i + 1 < len(anchors) else len(text)

            if anchor["type"] == "note_sub":
                safe_name = f"财务报表附注-{anchor['clean_name'].replace('/', '_')}"
                tier = "HIGH_RISK_NOTES" if any(k in safe_name for k in NOTES_HIGH_RISK_KEYWORDS) else "HIGH_FINANCIALS"
            else:
                safe_name = anchor["title"].replace('/', '_')
                tier = "HIGH_FINANCIALS"

            sub_blocks.append(
                self._make_sub_block(text, start, end, parent_seq, i + 1, parent_global_start, anchor["title"],
                                     safe_name, tier))

        return sub_blocks

    def _make_sub_block(self, text, start, end, parent_seq, sub_idx, parent_global_start, title, safe_name, tier):
        content = text[start:end]
        seq_str = f"{parent_seq}.{sub_idx:02d}"
        file_name = f"{seq_str}_{safe_name}.txt"
        file_path = self.blocks_dir / file_name
        file_path.write_text(content, encoding="utf-8")

        return asdict(
            TextBlock(seq_str, parent_global_start + start, parent_global_start + end, len(content), title, safe_name,
                      tier, f"blocks/{file_name}"))


# ==========================================
# 顶层流水线封装 (Facade)
# ==========================================
class AShareReportPipeline:
    def __init__(self, input_path: str, output_dir: str):
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.manifest = None

    def execute_stage2(self, model_name: str = "qwen2.5:14b-json") -> 'AShareReportPipeline':
        logger.info("-" * 40)
        logger.info("⚡ [Stage 2] 执行 L1 结构无损切割...")
        self.manifest = L1CoarseSplitter(self.input_path, self.output_dir, model_name).run()
        return self

    def execute_stage3(self) -> 'AShareReportPipeline':
        if not self.manifest: raise RuntimeError("必须先执行 stage2")
        logger.info("-" * 40)
        logger.info("⚡ [Stage 3] 执行 L2 财务报表与高危附注深切...")
        self.manifest = L2FinancialSplitter(self.output_dir, self.manifest).run()
        return self

    def save_manifest(self):
        manifest_path = self.output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"✅ 流水线完成！Manifest 索引表已写入: {manifest_path}")


# ==========================================
# 执行入口
# ==========================================

# python ashare_elt_pipeline.py "samples/000166_申万宏源_2025.txt" --out output/000166 --model qwen2.5:7b-instruct-q8_0-json

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("txt", help="输入的财报 txt 路径")
    parser.add_argument("--out", required=True, help="输出目录路径")
    parser.add_argument("--model", default="qwen2.5:14b-json", help="Ollama 模型标签")
    args = parser.parse_args()

    pipeline = (
        AShareReportPipeline(args.txt, args.out)
        .execute_stage2(model_name=args.model)
        .execute_stage3()
    )
    pipeline.save_manifest()