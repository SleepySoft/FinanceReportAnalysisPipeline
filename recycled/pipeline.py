import logging
from pathlib import Path

# 假设 L1 和 L2 的类已经存在当前命名空间，或者通过 import 导入
from lossless_report_splitter import LosslessFlatSplitter
from l2_financial_splitter import L2FinancialSplitter

logger = logging.getLogger(__name__)


class AShareReportPipeline:
    """
    A 股财报处理流水线 (Fluent Interface 链式调用设计)
    """

    def __init__(self, input_path: str, output_dir: str):
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.manifest = None
        logger.info(f"🚀 初始化财报流水线: {self.input_path.name}")

    def extract_l1_structure(self, model_name: str = "qwen2.5:14b-json") -> 'AShareReportPipeline':
        """第一步：LLM 动态脑图提取与 L1 无损粗切"""
        if not self.input_path.exists():
            raise FileNotFoundError(f"找不到输入文件: {self.input_path}")

        logger.info("-" * 40)
        logger.info("⚡ [Step 1] 执行 L1 一级结构无损切割...")

        splitter = LosslessFlatSplitter(self.input_path, self.output_dir, model_name=model_name)
        self.manifest = splitter.run()

        return self

    def deep_split_financials(self) -> 'AShareReportPipeline':
        """第二步：执行 L2 财务报告深水区切割"""
        if not self.manifest:
            raise RuntimeError("流水线异常：必须先执行 extract_l1_structure()，才能执行 L2 深切。")

        logger.info("-" * 40)
        logger.info("⚡ [Step 2] 执行 L2 财务报告高危附注深切...")

        # 将 L1 的产出目录传给 L2 模块
        l2_splitter = L2FinancialSplitter(self.output_dir)
        l2_splitter.run()

        # 更新流水线上下文中的 manifest
        self.manifest = l2_splitter.manifest
        return self

    def filter_by_value(self, target_tier: str) -> list:
        """附加功能：快速检索特定价值的数据块 (不改变 self，作为流水线终点)"""
        if not self.manifest:
            return []
        return [block for block in self.manifest.get("blocks", [])
                if block.get("value_tier") == target_tier]

    def get_manifest(self) -> dict:
        """获取最终的元数据表 (作为流水线终点)"""
        logger.info("-" * 40)
        logger.info("🏁 流水线全流程执行完毕！")
        return self.manifest


# ==========================================
# 顶层极简调用示例
# ==========================================

# python pipeline.py "samples/000166_申万宏源_2025.txt" --out output/000166 --model qwen2.5:7b-instruct-q8_0-json

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="A-Share Report ELT Pipeline")
    parser.add_argument("txt_file", type=str, help="输入的 A股财报 txt 路径")
    parser.add_argument("--out", type=str, required=True, help="输出目录路径")
    parser.add_argument("--model", type=str, default="qwen2.5:14b-json", help="Ollama 模型名")
    args = parser.parse_args()

    try:
        # 丝滑的链式调用语法 (Fluent Interface)
        pipeline = (
            AShareReportPipeline(input_path=args.txt_file, output_dir=args.out)
            .extract_l1_structure(model_name=args.model)  # L1 粗切
            .deep_split_financials()  # L2 财务细切
            # 未来可无限扩展：
            # .extract_tables_with_ocr()
            # .clean_noise_text()
        )

        # 流水线跑完后，下游业务直接按需取用数据
        final_manifest = pipeline.get_manifest()

        # 示例：业务端精准提纯，只拿关联交易和诉讼等高危节点去跑量化因子
        high_risk_blocks = pipeline.filter_by_value("HIGH_RISK_NOTES")

        print(f"\n📊 [业务提取完成] 发现 {len(high_risk_blocks)} 个极高价值(高危)模块：")
        for b in high_risk_blocks:
            print(f"   - {b['canonical_name']} (字符数: {b['char_count']})")

    except Exception as e:
        logger.error(f"❌ 流水线执行崩溃: {e}", exc_info=True)
