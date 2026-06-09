#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l2_financial_splitter.py

A-share L2 Deep Splitter: 专门针对“财务报告”进行外科手术式深切。
产出格式：007_01_审计报告.txt ... 007_06_财务报表附注-关联方及关联交易.txt
"""

import json
import logging
import re
import shutil
from pathlib import Path
from dataclasses import asdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 1. 高危附注价值识别字典
# ==========================================
NOTES_HIGH_RISK_KEYWORDS = ["关联方", "关联交易", "或有事项", "承诺", "日后事项", "风险", "诉讼"]


def get_notes_value_tier(heading_name: str) -> str:
    """根据附注标题名称判断价值等级"""
    if any(keyword in heading_name for keyword in NOTES_HIGH_RISK_KEYWORDS):
        return "HIGH_RISK_NOTES"  # 极高价值，下游优先进风控模型
    return "HIGH_FINANCIALS"


class L2FinancialSplitter:
    def __init__(self, processed_dir: str):
        self.processed_dir = Path(processed_dir)
        self.manifest_path = self.processed_dir / "manifest.json"
        self.blocks_dir = self.processed_dir / "blocks"

        with open(self.manifest_path, 'r', encoding='utf-8') as f:
            self.manifest = json.load(f)

    def run(self):
        logger.info("🚀 启动 L2 财务报告深切模块...")
        new_blocks = []
        financial_block_found = False

        for block in self.manifest["blocks"]:
            # 找到 L1 切割出的“财务报告”大块
            if block["canonical_name"] == "财务报告":
                financial_block_found = True
                logger.info(f"🎯 锁定目标: {block['file_path']}，字符数: {block['char_count']}")

                # 读取这个大块的原文
                file_path = self.processed_dir / block["file_path"]
                text = file_path.read_text(encoding="utf-8")

                # 执行深切
                sub_blocks = self._deep_split_financials(text, block["seq_id"], block["start_char"])
                new_blocks.extend(sub_blocks)

                # 切割成功后，删除原来的大文件，保持目录整洁
                file_path.unlink()
            else:
                # 其他块原样保留
                new_blocks.append(block)

        if not financial_block_found:
            logger.warning("未在 manifest 中找到“财务报告”块，跳过 L2 切割。")
            return

        # 更新并覆写 manifest.json
        self.manifest["blocks"] = new_blocks
        self.manifest["block_count"] = len(new_blocks)

        with open(self.manifest_path, 'w', encoding='utf-8') as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)

        logger.info("✅ L2 深切完成！Manifest 已更新。")

    def _deep_split_financials(self, text: str, parent_seq: int, parent_global_start: int) -> list:
        """
        对财务报告进行无损连续切割
        """
        anchors = []

        # 1. 寻找表头硬锚点 (固定顺序)
        hard_anchors = ["审计报告", "合并资产负债表", "合并利润表", "合并现金流量表", "合并所有者权益变动表",
                        "财务报表附注"]
        cursor = 0
        for title in hard_anchors:
            pos = text.find(title, cursor)
            if pos != -1:
                anchors.append({
                    "pos": pos,
                    "title": title,
                    "type": "main"
                })
                cursor = pos + len(title)

        # 2. 寻找附注内部的动态锚点 (正则探针)
        # 寻找诸如 "十一、关联方及关联交易" 的行。前后带换行符保证是独立标题。
        notes_pattern = re.compile(
            r'\n([一二三四五六七八九十十一十二十三十四十五十六十七十八十九二十]+、\s*([^。\n]{2,30}))(?=\n|$)')

        # 我们只在“财务报表附注”出现的位置之后去探测子附注，防止误伤前面的文字
        notes_start_pos = next((a["pos"] for a in anchors if a["title"] == "财务报表附注"), len(text))

        for match in notes_pattern.finditer(text, notes_start_pos):
            # match.group(1) 是完整标题，如 "十一、关联方及关联交易"
            # match.group(2) 是去掉了数字的纯净名字，如 "关联方及关联交易"
            full_title = match.group(1).strip()
            clean_name = match.group(2).strip()

            anchors.append({
                "pos": match.start() + 1,  # +1 是因为正则里有个 \n
                "title": full_title,
                "clean_name": clean_name,
                "type": "note_sub"
            })

        # 严格按照物理位置排序
        anchors = sorted(anchors, key=lambda x: x["pos"])

        # 3. 沿锚点执行无损切割
        sub_blocks = []
        first_pos = anchors[0]["pos"] if anchors else len(text)

        # 处理可能的“报告开头到第一个锚点”的前导文字
        if first_pos > 0:
            sub_blocks.append(self._create_sub_block(
                text=text, text_start=0, text_end=first_pos,
                parent_seq=parent_seq, sub_idx=0,
                parent_global_start=parent_global_start,
                title="财务报告_前言", safe_name="财务报告_前言", value_tier="MEDIUM"
            ))

        for i, anchor in enumerate(anchors):
            start = anchor["pos"]
            end = anchors[i + 1]["pos"] if i + 1 < len(anchors) else len(text)

            # 生成文件名后缀
            if anchor["type"] == "note_sub":
                # 附注内部的文件名：财务报表附注-关联方及关联交易
                safe_name = f"财务报表附注-{anchor['clean_name'].replace('/', '_')}"
                value_tier = get_notes_value_tier(anchor["clean_name"])
            else:
                # 主表文件名：合并资产负债表
                safe_name = anchor["title"].replace('/', '_')
                value_tier = "HIGH_FINANCIALS"

            sub_blocks.append(self._create_sub_block(
                text=text, text_start=start, text_end=end,
                parent_seq=parent_seq, sub_idx=i + 1,
                parent_global_start=parent_global_start,
                title=anchor["title"], safe_name=safe_name, value_tier=value_tier
            ))

        # 4. 无损性断言 (防丢数据)
        reconstructed = "".join(
            [text[b["start_char"] - parent_global_start: b["end_char"] - parent_global_start] for b in sub_blocks])
        assert len(reconstructed) == len(text), "L2 深切发生数据丢失！"

        return sub_blocks

    def _create_sub_block(self, text: str, text_start: int, text_end: int,
                          parent_seq: int, sub_idx: int, parent_global_start: int,
                          title: str, safe_name: str, value_tier: str) -> dict:

        content = text[text_start:text_end]
        global_start = parent_global_start + text_start
        global_end = parent_global_start + text_end

        # 组合出 L2 的编号，例如原 财务报告 是 007，现在变成 007.01, 007.02
        seq_str = f"{parent_seq:03d}.{sub_idx:02d}"
        file_name = f"{seq_str}_{safe_name}.txt"
        file_path = self.blocks_dir / file_name

        file_path.write_text(content, encoding="utf-8")

        return {
            "seq_id": seq_str,
            "start_char": global_start,
            "end_char": global_end,
            "char_count": len(content),
            "raw_title": title,
            "canonical_name": safe_name,  # 直接用带“-”的名称作为典范名
            "value_tier": value_tier,
            "file_path": f"blocks/{file_name}"
        }


# ==========================================
# 独立执行入口
# ==========================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="A-Share L2 Financial Notes Splitter")
    parser.add_argument("processed_dir", type=str, help="L1 处理完成后的产出目录 (包含 manifest.json)")

    args = parser.parse_args()

    splitter = L2FinancialSplitter(args.processed_dir)
    splitter.run()