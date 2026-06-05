#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lossless_report_splitter.py

A-share annual report lossless flat splitter with LLM Structured Outputs.
架构模式：ELT (无损平铺提取 -> 生成价值索引 -> 下游按需清洗)
"""

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Optional

import ollama
from pydantic import BaseModel, ValidationError

# ==========================================
# 0. 日志配置
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==========================================
# 1. Pydantic 约束模型与价值体系
# ==========================================
class Section(BaseModel):
    order: int
    raw_title: str
    canonical_name: Optional[str]


class ReportTOC(BaseModel):
    sections: List[Section]


VALUE_TIER_MAP = {
    "管理层讨论与分析": "HIGH",
    "财务报告": "HIGH",
    "财务报表附注": "HIGH",
    "重要事项": "MEDIUM",
    "公司治理": "MEDIUM",
    "股份变动及股东情况": "MEDIUM",
    "债券相关情况": "MEDIUM",
    "公司简介和主要财务指标": "LOW",
    "重要提示_目录_释义": "LOW",
    "环境和社会责任": "LOW"
}


@dataclass
class TextBlock:
    seq_id: int
    start_char: int
    end_char: int
    char_count: int
    raw_title: str
    canonical_name: str
    value_tier: str
    file_path: str


# ==========================================
# 2. LLM 结构化输出提取器
# ==========================================
class LLMTocExtractor:
    def __init__(self, model_name: str = "qwen2.5:14b-json"):
        self.model_name = model_name

    def extract_toc(self, text: str, head_chars: int = 8000) -> List[Section]:
        """截取头部文本，交由 Ollama 进行物理级约束提取"""
        head_text = text[:head_chars]

        prompt = f"""
        你是一个专业的金融数据提取 API。请阅读以下 A 股财报头部文本，提取出该财报的目录结构。
        标准名称(canonical_name)请尽可能映射到以下范围，若不在范围请输出 null：
        ["重要提示_目录_释义", "公司简介和主要财务指标", "管理层讨论与分析", "公司治理", "环境和社会责任", "重要事项", "股份变动及股东情况", "债券相关情况", "财务报告"]

        财报文本：
        {head_text}
        """

        logger.info(f"正在调用本地大模型 [{self.model_name}] 提取目录地图...")
        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{'role': 'user', 'content': prompt}],
                format=ReportTOC.model_json_schema(),
                options={
                    "temperature": 0.0,
                    "num_ctx": 8192
                }
            )

            # Pydantic 终极校验
            parsed_data = ReportTOC.model_validate_json(response['message']['content'])
            logger.info(f"✅ 成功提取到 {len(parsed_data.sections)} 个一级章节锚点")

            # 过滤掉无法识别或没有标准名称的噪音章节
            valid_sections = [s for s in parsed_data.sections if s.canonical_name]
            return sorted(valid_sections, key=lambda x: x.order)

        except ValidationError as ve:
            logger.error(f"❌ 格式验证失败，大模型输出了非预期的结构: {ve}")
            return []
        except Exception as e:
            logger.error(f"❌ 大模型调用异常: {e}")
            return []


# ==========================================
# 3. 无损切割与打标核心类
# ==========================================
class LosslessFlatSplitter:
    def __init__(self, input_path: Path, output_dir: Path, model_name: str = "qwen2.5:14b-json"):
        self.input_path = input_path
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.doc_id = self.input_path.stem
        self.extractor = LLMTocExtractor(model_name=model_name)

    def _read_text(self) -> str:
        return self.input_path.read_text(encoding="utf-8", errors="ignore")

    def _find_anchor_positions(self, text: str, toc_list: List[Section]) -> List[Dict]:
        """
        在正文中寻找锚点的实际物理坐标。
        核心算法：Look-ahead 距离探测防伪机制。
        """
        anchors = []
        cursor = 0
        # 物理阈值：如果两个章节标题相距不到 300 字符，100% 是在目录页或正文的某句话里被提及了
        TOC_GAP_THRESHOLD = 300

        for i, toc in enumerate(toc_list):
            while True:
                pos = text.find(toc.raw_title, cursor)
                if pos == -1:
                    logger.warning(f"未能找到章节正文落点: {toc.raw_title}")
                    break

                # Look-ahead 探测机制：检查它和下一个章节的距离
                if i + 1 < len(toc_list):
                    next_toc = toc_list[i + 1]
                    next_pos = text.find(next_toc.raw_title, pos + len(toc.raw_title))

                    if next_pos != -1 and (next_pos - pos) < TOC_GAP_THRESHOLD:
                        # 触发防伪报警！距离太近，说明当前 pos 匹配到的还是目录页的内容
                        # 拒绝收录，游标往后推，继续寻找当前章节在正文中的真正位置
                        logger.debug(f"跳过伪锚点(目录区/引用): {toc.raw_title} @ {pos}")
                        cursor = pos + len(toc.raw_title)
                        continue

                # 恭喜，通过防伪探测！这才是真身。
                anchors.append({
                    "pos": pos,
                    "raw_title": toc.raw_title,
                    "canonical_name": toc.canonical_name
                })
                # 确认了真实位置后，把全局游标推到这里，保证后续切割绝对保序
                cursor = pos + len(toc.raw_title)
                break

        return anchors

    def run(self):
        logger.info(f"开始处理财报: {self.doc_id}")
        text = self._read_text()

        # 1. 抽取骨架
        toc_list = self.extractor.extract_toc(text)
        if not toc_list:
            logger.error("未能提取到目录，放弃切分任务。")
            return None

        # 2. 定位坐标
        anchors = self._find_anchor_positions(text, toc_list)

        blocks: List[TextBlock] = []
        blocks_dir = self.output_dir / "blocks"
        blocks_dir.mkdir(exist_ok=True)

        # 3. 第一刀：切下头部前言（0 到第一个锚点）
        first_pos = anchors[0]["pos"] if anchors else len(text)
        if first_pos > 0:
            blocks.append(self._create_block(
                text=text, seq_id=0, start=0, end=first_pos,
                raw_title="封面与目录", canonical_name="_PREAMBLE", blocks_dir=blocks_dir
            ))

        # 4. 循环切块
        for i, anchor in enumerate(anchors):
            start = anchor["pos"]
            end = anchors[i + 1]["pos"] if i + 1 < len(anchors) else len(text)

            blocks.append(self._create_block(
                text=text, seq_id=i + 1, start=start, end=end,
                raw_title=anchor["raw_title"],
                canonical_name=anchor["canonical_name"],
                blocks_dir=blocks_dir
            ))

        # 5. 断言验证
        self._verify_lossless(text, blocks)

        # 6. 生成 Manifest 数据表
        manifest_path = self.output_dir / "manifest.json"
        manifest_data = {
            "doc_id": self.doc_id,
            "total_chars": len(text),
            "block_count": len(blocks),
            "is_lossless_verified": True,
            "blocks": [asdict(b) for b in blocks]
        }
        manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(f"✅ 切分成功！产出 {len(blocks)} 块。Manifest 已生成。")
        return manifest_data

    def _create_block(self, text: str, seq_id: int, start: int, end: int,
                      raw_title: str, canonical_name: str, blocks_dir: Path) -> TextBlock:
        content = text[start:end]
        value_tier = VALUE_TIER_MAP.get(canonical_name, "UNMAPPED")

        # 避免 Windows 路径不允许的字符
        safe_name = f"{seq_id:03d}_{canonical_name.replace('/', '_')}"
        file_path = blocks_dir / f"{safe_name}.txt"
        file_path.write_text(content, encoding="utf-8", errors="ignore")

        return TextBlock(
            seq_id=seq_id, start_char=start, end_char=end,
            char_count=len(content), raw_title=raw_title,
            canonical_name=canonical_name, value_tier=value_tier,
            file_path=f"blocks/{safe_name}.txt"
        )

    def _verify_lossless(self, original_text: str, blocks: List[TextBlock]):
        """核心守卫：如果拼接后和原文不一致，程序必须崩溃并报警"""
        reconstructed = "".join([original_text[b.start_char:b.end_char] for b in blocks])
        assert len(reconstructed) == len(original_text), "字节长度效验失败！发生数据丢失。"
        assert reconstructed == original_text, "全量文本比对失败！"
        logger.info("🔒 无损拼接效验通过 (Lossless Verified).")


# ==========================================
# 测试入口
# ==========================================

# python lossless_report_splitter.py "samples/000166_申万宏源_2025.txt" --out output/000166 --model qwen2.5:7b-instruct-q8_0-json

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="A-Share Report Lossless ELT Splitter")
    parser.add_argument("txt_file", type=str, help="输入的 A股财报 txt 路径")
    parser.add_argument("--out", type=str, required=True, help="输出目录路径")
    parser.add_argument("--model", type=str, default="qwen2.5:14b-json", help="使用的 Ollama API 模型")

    args = parser.parse_args()

    input_file = Path(args.txt_file)
    output_dir = Path(args.out)

    if not input_file.exists():
        logger.error(f"找不到输入文件: {input_file}")
    else:
        splitter = LosslessFlatSplitter(input_file, output_dir, model_name=args.model)
        splitter.run()
