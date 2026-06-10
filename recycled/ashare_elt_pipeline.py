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
from typing import List, Dict, Optional, Tuple
from collections import Counter

import ollama
from pydantic import BaseModel, ValidationError

from report_type_detector import ReportTypeDetector, DetectionResult

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
    "环境和社会责任": "LOW",
    "债券相关情况": "LOW",
    "重要提示_目录_释义": "LOW",
    "公司简介和主要财务指标": "LOW",
    "_PREAMBLE": "UNMAPPED"
}

# ==========================================
# 1. 数据模型
# ==========================================
@dataclass
class TextBlock:
    seq_id: str
    start_char: int
    end_char: int
    char_count: int
    title: str
    canonical_name: str
    value_tier: str
    file_path: str


# ==========================================
# 2. 阶段 1: 文本清洗
# ==========================================
class Stage1TextCleaner:
    """TXT-first 清洗管线"""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir
        self.stats = {
            "original_chars": 0,
            "cleaned_chars": 0,
            "removed_lines": 0,
            "removed_chars": 0,
            "steps": []
        }
    
    def clean(self, text: str, doc_id: str = "unknown") -> Tuple[str, dict]:
        """执行完整清洗流程"""
        self.stats["original_chars"] = len(text)
        original_lines = text.split('\n')
        
        # Step 0: 删除竖排文字
        text = self._remove_vertical_text(text)
        self.stats["steps"].append({"step": "remove_vertical_text", "chars_after": len(text)})
        
        # Step 1: 编码和乱码检查
        text = self._fix_encoding(text)
        self.stats["steps"].append({"step": "fix_encoding", "chars_after": len(text)})
        
        # Step 2: 删除页眉页脚
        text, header_footer_stats = self._remove_headers_footers(text)
        self.stats["steps"].append({"step": "remove_headers_footers", "chars_after": len(text), **header_footer_stats})
        
        # Step 3: 删除页码和目录页码线
        text, page_num_stats = self._remove_page_numbers(text)
        self.stats["steps"].append({"step": "remove_page_numbers", "chars_after": len(text), **page_num_stats})
        
        # Step 4: 删除连续空行
        text, blank_stats = self._remove_excess_blank_lines(text)
        self.stats["steps"].append({"step": "remove_blank_lines", "chars_after": len(text), **blank_stats})
        
        # Step 5: 规范标点和空白字符
        text = self._normalize_punctuation(text)
        self.stats["steps"].append({"step": "normalize_punctuation", "chars_after": len(text)})
        
        # Step 6: 质量校验
        quality = self._quality_check(text)
        self.stats["steps"].append({"step": "quality_check", "quality_score": quality["score"], "issues": quality["issues"]})
        
        self.stats["cleaned_chars"] = len(text)
        self.stats["removed_chars"] = self.stats["original_chars"] - self.stats["cleaned_chars"]
        self.stats["removed_lines"] = len(original_lines) - len(text.split('\n'))
        
        # 保存清洗报告
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            report_path = self.output_dir / f"{doc_id}_clean_report.json"
            report_path.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"📝 清洗报告已保存: {report_path}")
        
        return text, self.stats
    
    def _remove_vertical_text(self, text: str) -> str:
        """删除所有竖排文字（每行1-2个字符）"""
        lines = text.split('\n')
        result = []
        removed_count = 0
        
        for line in lines:
            stripped = line.strip()
            if 1 <= len(stripped) <= 2 and stripped:
                # 检查是否是中文、英文或数字
                if re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9]+$', stripped):
                    # 删除竖排文字
                    removed_count += 1
                    continue
            result.append(line)
        
        if removed_count > 0:
            logger.info(f"🗑️ 删除 {removed_count} 处竖排文字")
        
        return '\n'.join(result)
    
    def _merge_vertical_text(self, text: str) -> str:
        """合并竖排文字为横排，便于后续识别（保留版本）"""
        # 先尝试删除，如果删除后质量不好，可以切换为合并
        return self._remove_vertical_text(text)
    
    def clean(self, text: str, doc_id: str = "unknown") -> Tuple[str, dict]:
        """执行完整清洗流程"""
        self.stats["original_chars"] = len(text)
        original_lines = text.split('\n')
        
        # Step 0: 删除竖排文字
        text = self._remove_vertical_text(text)
        self.stats["steps"].append({"step": "remove_vertical_text", "chars_after": len(text)})
        
        # Step 1: 编码和乱码检查
        text = self._fix_encoding(text)
        self.stats["steps"].append({"step": "fix_encoding", "chars_after": len(text)})
        
        # Step 2: 删除页眉页脚
        text, header_footer_stats = self._remove_headers_footers(text)
        self.stats["steps"].append({"step": "remove_headers_footers", "chars_after": len(text), **header_footer_stats})
        
        # Step 3: 删除页码和目录页码线
        text, page_num_stats = self._remove_page_numbers(text)
        self.stats["steps"].append({"step": "remove_page_numbers", "chars_after": len(text), **page_num_stats})
        
        # Step 4: 删除连续空行
        text, blank_stats = self._remove_excess_blank_lines(text)
        self.stats["steps"].append({"step": "remove_blank_lines", "chars_after": len(text), **blank_stats})
        
        # Step 5: 规范标点和空白字符
        text = self._normalize_punctuation(text)
        self.stats["steps"].append({"step": "normalize_punctuation", "chars_after": len(text)})
        
        # Step 6: 质量校验
        quality = self._quality_check(text)
        self.stats["steps"].append({"step": "quality_check", "quality_score": quality["score"], "issues": quality["issues"]})
        
        self.stats["cleaned_chars"] = len(text)
        self.stats["removed_chars"] = self.stats["original_chars"] - self.stats["cleaned_chars"]
        self.stats["removed_lines"] = len(original_lines) - len(text.split('\n'))
        
        # 保存清洗报告
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            report_path = self.output_dir / f"{doc_id}_clean_report.json"
            report_path.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"📝 清洗报告已保存: {report_path}")
        
        return text, self.stats
    
    def _fix_encoding(self, text: str) -> str:
        """修复编码问题，处理 (cid:xx) 乱码"""
        cid_count = len(re.findall(r'\(cid:\d+\)', text))
        if cid_count > 100:
            logger.warning(f"⚠️ 发现 {cid_count} 个 (cid:xx) 乱码标记，PDF 转 TXT 质量可能较差")
        return text
    
    def _remove_headers_footers(self, text: str) -> Tuple[str, dict]:
        """删除页眉页脚"""
        lines = text.split('\n')
        line_counts = Counter(line.strip() for line in lines if len(line.strip()) > 3)
        
        # 识别高频行（可能是页眉/页脚）
        header_candidates = {}
        for line, count in line_counts.items():
            if count > 5 and 10 <= len(line) <= 60:
                if not re.search(r'第[一二三四五六七八九十]+节', line):
                    header_candidates[line] = count
        
        # 页眉模式
        header_patterns = [
            r'202[5-6].*年度报告',
            r'年度报告.*202[5-6]',
            r'^\s*\d+\s*$',  # 纯数字行
            r'股份有限公司.*202',
            r'第[一二三四五六七八九十]+节.*（续）',
        ]
        
        removed_lines = []
        cleaned_lines = []
        
        for line in lines:
            stripped = line.strip()
            is_header = False
            
            if stripped in header_candidates:
                is_header = True
            
            for pattern in header_patterns:
                if re.search(pattern, stripped):
                    is_header = True
                    break
            
            # 关键：保留真正的章节标题（不带"续"的）
            if re.search(r'第[一二三四五六七八九十]+节\s+[^\n（续）]{2,30}', stripped):
                # 检查是否包含"续"（页眉标记）
                if '（续）' in stripped:
                    is_header = True
                # 检查后面是否跟着页码（目录页格式）
                elif not re.search(r'\d{2,}\s*$', stripped):
                    is_header = False
            
            if is_header:
                cleaned_lines.append('\n')  # 替换为换行
                removed_lines.append(stripped)
            else:
                cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines), {
            "removed_header_lines": len(removed_lines),
            "header_candidates_found": len(header_candidates)
        }
    
    def _remove_page_numbers(self, text: str) -> Tuple[str, dict]:
        """删除页码和目录页码线"""
        lines = text.split('\n')
        cleaned_lines = []
        removed_lines = []
        
        for line in lines:
            stripped = line.strip()
            
            # 纯数字（页码）
            if re.match(r'^\d+$', stripped) and len(stripped) <= 3:
                removed_lines.append(stripped)
                continue
            
            # 目录页码线：章节名 + 大量空格/点 + 数字
            if re.match(r'^.+\.{3,}\d+$', stripped):
                removed_lines.append(stripped)
                continue
            
            # 目录页章节列表：第X节 章节名 + 空格 + 数字（页码）
            if re.match(r'第[一二三四五六七八九十]+节\s+.+\s+\d+$', stripped):
                removed_lines.append(stripped)
                continue
            
            cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines), {
            "removed_page_num_lines": len(removed_lines)
        }
    
    def _remove_excess_blank_lines(self, text: str) -> Tuple[str, dict]:
        """删除连续空行，保留最多 2 个连续空行"""
        lines = text.split('\n')
        cleaned_lines = []
        blank_count = 0
        removed_count = 0
        
        for line in lines:
            if line.strip() == '':
                blank_count += 1
                if blank_count <= 2:
                    cleaned_lines.append(line)
                else:
                    removed_count += 1
            else:
                blank_count = 0
                cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines), {
            "removed_excess_blank_lines": removed_count
        }
    
    def _normalize_punctuation(self, text: str) -> str:
        """规范标点和空白字符"""
        # 统一空格
        text = re.sub(r'[ \t]+', ' ', text)
        
        # 删除行尾空格
        text = re.sub(r' +\n', '\n', text)
        
        return text
    
    def _quality_check(self, text: str) -> dict:
        """质量校验"""
        issues = []
        score = 100
        
        # 检查乱码比例
        cid_count = len(re.findall(r'\(cid:\d+\)', text))
        cid_ratio = cid_count / max(len(text), 1) * 1000
        if cid_ratio > 1:
            issues.append(f"乱码比例过高: {cid_ratio:.2f}‰ ({cid_count} 个)")
            score -= min(30, int(cid_ratio * 3))
        
        # 检查空行比例
        blank_ratio = text.count('\n\n') / max(text.count('\n'), 1)
        if blank_ratio > 0.5:
            issues.append(f"空行比例过高: {blank_ratio:.2%}")
            score -= 10
        
        # 检查平均行长度
        lines = text.split('\n')
        avg_len = sum(len(line) for line in lines) / max(len(lines), 1)
        if avg_len < 20:
            issues.append(f"平均行长度过短: {avg_len:.1f} 字符")
            score -= 10
        
        # 检查是否包含章节标题
        has_sections = bool(re.search(r'第[一二三四五六七八九十]+节', text))
        if not has_sections:
            issues.append("未检测到章节标题")
            score -= 20
        
        return {
            "score": max(0, score),
            "issues": issues,
            "cid_count": cid_count,
            "blank_ratio": blank_ratio,
            "avg_line_length": avg_len,
            "has_sections": has_sections
        }


# ==========================================
# 3. 阶段 2: L1 粗切 (正则 + LLM 双保险)
# ==========================================
class L1CoarseSplitter:
    """
    L1 粗切：正则预筛选 + LLM 精确认证
    """
    
    # 标准章节定义
    SECTION_DEFINITIONS = {
        "重要提示_目录_释义": {
            "aliases": ["重要提示", "目录", "释义", "备查文件"],
            "description": "年报开头部分，包含重要提示、目录和释义"
        },
        "公司简介和主要财务指标": {
            "aliases": ["公司简介", "主要财务指标", "公司信息"],
            "description": "公司基本信息和主要财务数据"
        },
        "管理层讨论与分析": {
            "aliases": ["管理层讨论与分析", "经营情况讨论", "董事会报告"],
            "description": "管理层对经营情况的讨论和分析"
        },
        "公司治理": {
            "aliases": ["公司治理", "股东大会", "董事会", "监事会"],
            "description": "公司治理结构和运作情况"
        },
        "环境和社会责任": {
            "aliases": ["环境", "社会责任", "ESG", "环境保护"],
            "description": "环境保护和社会责任履行情况"
        },
        "重要事项": {
            "aliases": ["重要事项", "重大事项", "利润分配", "关联交易"],
            "description": "报告期内重要事项"
        },
        "股份变动及股东情况": {
            "aliases": ["股份变动", "股东情况", "股本变动", "股东总数"],
            "description": "股份变动和股东持股情况"
        },
        "债券相关情况": {
            "aliases": ["债券", "可转债", "公司债券"],
            "description": "公司债券发行和偿还情况"
        },
        "财务报告": {
            "aliases": ["财务报告", "审计报告", "财务报表", "合并资产负债表"],
            "description": "经审计的财务报表和附注"
        }
    }
    
    # 内容关键词匹配模式 - 用于定位章节大致位置
    # 注意：只匹配真正的章节标题，不匹配正文中的引用
    CONTENT_PATTERNS = {
        "重要提示_目录_释义": [
            r"第[一二三四五六七八九十]+节\s+重要提示",
            r"第[一二三四五六七八九十]+节\s+释义",
            r"重要提示、目录和释义",
        ],
        "公司简介和主要财务指标": [
            r"第[一二三四五六七八九十]+节\s+公司简介",
            r"第[一二三四五六七八九十]+节\s+主要财务指标",
        ],
        "管理层讨论与分析": [
            r"第[一二三四五六七八九十]+节\s+管理层讨论",
            r"第[一二三四五六七八九十]+节\s+经营情况讨论",
        ],
        "公司治理": [
            r"第[一二三四五六七八九十]+节\s+公司治理",
        ],
        "环境和社会责任": [
            r"第[一二三四五六七八九十]+节\s+环境",
            r"第[一二三四五六七八九十]+节\s+社会责任",
        ],
        "重要事项": [
            r"第[一二三四五六七八九十]+节\s+重要事项",
            r"第[一二三四五六七八九十]+节\s+重大事项",
        ],
        "股份变动及股东情况": [
            r"第[一二三四五六七八九十]+节\s+股份变动",
            r"第[一二三四五六七八九十]+节\s+股东情况",
        ],
        "债券相关情况": [
            r"第[一二三四五六七八九十]+节\s+债券",
        ],
        "财务报告": [
            r"第[一二三四五六七八九十]+节\s+财务报告",
            r"第[一二三四五六七八九十]+节\s+审计报告",
        ],
    }
    
    # 预期位置范围（字符位置）
    EXPECTED_RANGES = {
        "重要提示_目录_释义": (0, 10000),
        "公司简介和主要财务指标": (0, 20000),
        "管理层讨论与分析": (10000, 100000),
        "公司治理": (50000, 150000),
        "环境和社会责任": (50000, 150000),
        "重要事项": (100000, 200000),
        "股份变动及股东情况": (200000, 300000),
        "债券相关情况": (200000, 350000),
        "财务报告": (200000, 500000),
    }
    
    # 分析区域配置
    CANDIDATE_RADIUS = 300  # 候选点前后搜索半径
    REGION_MERGE_DISTANCE = 5000  # 合并相邻区域的距离阈值
    
    def __init__(self, input_path: Path, output_dir: Path, model_name: str = "qwen2.5:7b-instruct-q8_0-json"):
        self.input_path = input_path
        self.output_dir = output_dir
        self.doc_id = input_path.stem
        self.model_name = model_name
    
    def _find_candidates_with_regex(self, text: str) -> List[int]:
        """用内容关键词定位章节大致位置"""
        candidates = []
        
        for canonical_name, patterns in self.CONTENT_PATTERNS.items():
            expected_start, expected_end = self.EXPECTED_RANGES.get(canonical_name, (0, len(text)))
            
            for pattern in patterns:
                for match in re.finditer(pattern, text):
                    pos = match.start()
                    
                    # 位置初筛：必须在预期范围内
                    if pos < expected_start or pos > expected_end:
                        continue
                    
                    # 过滤目录页区域（前5000字符内只保留第一个匹配）
                    if pos < 5000 and any(abs(pos - c) < 1000 for c in candidates):
                        continue
                    
                    # 过滤页眉重复（距离已有候选点 < 2000）
                    if any(abs(pos - c) < 2000 and c < pos for c in candidates):
                        continue
                    
                    candidates.append(pos)
                    logger.debug(f"🔍 候选: {canonical_name} @ {pos:,}")
                    break  # 该章节已找到候选点，跳过其他模式
        
        # 去重并排序
        candidates = sorted(set(candidates))
        logger.info(f"🔍 正则预筛选发现 {len(candidates)} 个候选点")
        return candidates
    
    def _merge_candidates_to_regions(self, candidates: List[int], text_len: int) -> List[tuple]:
        """将候选点合并成连续区域"""
        if not candidates:
            return []
        
        regions = []
        start = candidates[0]
        end = candidates[0]
        
        for pos in candidates[1:]:
            if pos - end <= self.REGION_MERGE_DISTANCE:
                # 合并到当前区域
                end = pos
            else:
                # 保存当前区域，开始新区域
                regions.append((
                    max(0, start - self.CANDIDATE_RADIUS),
                    min(end + self.CANDIDATE_RADIUS, text_len)
                ))
                start = pos
                end = pos
        
        # 保存最后一个区域
        regions.append((
            max(0, start - self.CANDIDATE_RADIUS),
            min(end + self.CANDIDATE_RADIUS, text_len)
        ))
        
        logger.info(f"🔄 合并为 {len(regions)} 个分析区域")
        for i, (s, e) in enumerate(regions):
            logger.info(f"   区域 {i+1}: {s:,} - {e:,} (长度: {e-s:,})")
        
        return regions
    
    def _build_region_prompt(self, text: str, region_start: int, region_end: int) -> str:
        """构建区域分析 prompt"""
        region_text = text[region_start:region_end]
        
        section_list = "\n".join([
            f"- {name}: {info['description']}\n  关键词: {', '.join(info['aliases'][:3])}"
            for name, info in self.SECTION_DEFINITIONS.items()
        ])
        
        return f"""你是一个专业的金融文档结构分析 API。请分析以下文本区域，判断其中是否包含 A 股年报的章节边界。

## 分析区域位置
文档第 {region_start:,} - {region_end:,} 字符

## 标准章节定义
{section_list}

## 判断规则
1. **真正的章节边界**必须同时满足：
   - 包含明确的章节标题（如"第X节 XXX"或"XXX"单独成行）
   - 标题后跟着大量正文内容（不是目录页）
   - 标题在文档中单独出现，不是被引用（如"详见第X节"）

2. **特别注意：区分标题和引用**
   - ❌ 标题："第八节 财务报告"（单独成行，后面跟着正文）
   - ✅ 引用："参见第八节财务报告之..."（在句子中间，不是标题）
   - ❌ 标题："财务报告"（单独成行）
   - ✅ 引用："按照中国会计准则披露的财务报告中..."（在句子中间）

3. **不是边界的情况**：
   - 目录页中的章节列表
   - 正文中间引用其他章节（"详见第X节 XXX"）
   - 页眉页脚中的重复文字
   - 表格内的文字

4. **输出要求**：
   - 返回该区域中所有真正的章节边界
   - 每个边界包含：标题文字、映射后的标准名称
   - 如果没有边界，返回空数组 []

## 输出格式
严格返回 JSON 数组：
[{{"raw_title": "原文标题", "canonical_name": "标准名称"}}]

## 待分析文本
{region_text}
"""
    
    def _analyze_regions_with_llm(self, text: str, regions: List[tuple]) -> List[Dict]:
        """用 LLM 分析每个区域，识别真正的章节边界"""
        boundaries = []
        
        for i, (start, end) in enumerate(regions):
            prompt = self._build_region_prompt(text, start, end)
            
            try:
                # 使用简单 JSON 格式，不需要 Pydantic schema
                response = ollama.chat(
                    model=self.model_name,
                    messages=[{'role': 'user', 'content': prompt}],
                    options={"temperature": 0.0, "num_ctx": 4096}
                )
                
                content = response['message']['content']
                
                # 提取 JSON 数组
                # 尝试多种格式
                json_str = None
                
                # 尝试从代码块中提取
                if '```json' in content:
                    json_str = content.split('```json')[1].split('```')[0].strip()
                elif '```' in content:
                    json_str = content.split('```')[1].split('```')[0].strip()
                else:
                    # 尝试直接提取 JSON 数组
                    start_idx = content.find('[')
                    end_idx = content.rfind(']')
                    if start_idx != -1 and end_idx != -1:
                        json_str = content[start_idx:end_idx+1]
                
                if json_str:
                    results = json.loads(json_str)
                    for result in results:
                        if 'raw_title' in result and 'canonical_name' in result:
                            pos = text.find(result['raw_title'], start)
                            if pos == -1:
                                pos = start
                            
                            boundaries.append({
                                "pos": pos,
                                "raw_title": result['raw_title'],
                                "canonical_name": result['canonical_name'],
                                "confidence": 0.9
                            })
                            logger.info(f"✅ 区域 {i+1} 发现边界: {result['canonical_name']} @ {pos:,}")
                
            except Exception as e:
                logger.warning(f"⚠️ 区域 {i+1} 分析失败: {e}")
                continue
        
        return boundaries
    
    def _merge_nearby_boundaries(self, boundaries: List[Dict], min_distance: int = 5000) -> List[Dict]:
        """合并距离过近的边界（去重），限制每个标准章节只出现一次"""
        if not boundaries:
            return []
        
        # 按位置排序
        sorted_boundaries = sorted(boundaries, key=lambda x: x["pos"])
        
        # 先按距离合并（但只合并相同 canonical_name 的边界）
        merged = [sorted_boundaries[0]]
        for b in sorted_boundaries[1:]:
            last = merged[-1]
            if b["canonical_name"] == last["canonical_name"] and b["pos"] - last["pos"] < min_distance:
                # 相同章节，距离太近，保留置信度更高的
                if b["confidence"] > last["confidence"]:
                    merged[-1] = b
            else:
                merged.append(b)
        
        # 再按 canonical_name 去重，保留位置最靠前的
        seen_canonical = {}
        for b in merged:
            c = b["canonical_name"]
            if c not in seen_canonical or b["confidence"] > seen_canonical[c]["confidence"]:
                seen_canonical[c] = b
        
        # 按位置排序返回
        result = sorted(seen_canonical.values(), key=lambda x: x["pos"])
        logger.info(f"🔄 按章节名去重后: {len(result)} 个边界")
        for b in result:
            logger.info(f"   {b['canonical_name']} @ {b['pos']:,} (置信度: {b['confidence']:.2f})")
        
        return result
    
    def _fill_missing_sections(self, boundaries: List[Dict], text: str) -> List[Dict]:
        """补充缺失的标准章节，但只在预期范围内搜索"""
        found_canonical = {b["canonical_name"] for b in boundaries}
        missing = set(self.SECTION_DEFINITIONS.keys()) - found_canonical
        
        for canonical in missing:
            expected_start, expected_end = self.EXPECTED_RANGES.get(canonical, (0, len(text)))
            aliases = self.SECTION_DEFINITIONS[canonical]["aliases"]
            
            for alias in aliases:
                patterns = [
                    rf'第[一二三四五六七八九十]+节\s+{re.escape(alias)}',
                    rf'^[\s]*{re.escape(alias)}[\s]*$',
                ]
                for pattern in patterns:
                    for match in re.finditer(pattern, text, re.MULTILINE):
                        pos = match.start()
                        # 必须在预期范围内
                        if pos < expected_start or pos > expected_end:
                            continue
                        # 不能和已有边界太近
                        if any(abs(pos - b["pos"]) < 10000 for b in boundaries):
                            continue
                        boundaries.append({
                            "pos": pos,
                            "raw_title": alias,
                            "canonical_name": canonical,
                            "confidence": 0.5
                        })
                        logger.info(f"🔧 正则补充: {canonical} @ {pos:,}")
                        break
                    else:
                        continue
                    break
                else:
                    continue
                break
        
        return sorted(boundaries, key=lambda x: x["pos"])
    
    def _split_merged_titles(self, boundaries: List[Dict], text: str) -> List[Dict]:
        """拆分合并标题，如'公司治理、环境和社会'拆分为两个边界"""
        # 已知的合并标题模式
        MERGED_PATTERNS = {
            "公司治理、环境和社会": ["公司治理", "环境和社会责任"],
            "公司治理、环境和社会责任": ["公司治理", "环境和社会责任"],
        }
        
        result = []
        for b in boundaries:
            raw = b["raw_title"]
            # 检查是否匹配已知的合并标题
            merged = False
            for pattern, parts in MERGED_PATTERNS.items():
                if pattern in raw:
                    merged = True
                    for i, part in enumerate(parts):
                        # 搜索该部分的真实位置
                        search_pos = self._find_section_position(text, part, b["pos"], b["pos"] + 100000)
                        
                        if search_pos is None:
                            search_pos = b["pos"] + i * 100  # 回退到估算位置
                        
                        canonical = self._find_canonical_name(part)
                        if canonical:
                            result.append({
                                "pos": search_pos,
                                "raw_title": part,
                                "canonical_name": canonical,
                                "confidence": b["confidence"] * 0.9
                            })
                    break
            
            if not merged:
                result.append(b)
        
        # 按位置排序
        return sorted(result, key=lambda x: x["pos"])
    
    def _find_section_position(self, text: str, section_name: str, start_pos: int, end_pos: int) -> Optional[int]:
        """在文本中搜索章节的真实位置"""
        search_text = text[start_pos:end_pos]
        
        # 尝试多种搜索模式
        search_patterns = [
            rf'第[一二三四五六七八九十]+节\s+{re.escape(section_name)}',  # 第X节 标题
            rf'[一二三四五六七八九十]+、\s*{re.escape(section_name)}',  # 一、标题
            rf'^{re.escape(section_name)}$',  # 单独标题
        ]
        
        for search_pattern in search_patterns:
            matches = list(re.finditer(search_pattern, search_text, re.MULTILINE))
            # 过滤掉包含"续"的匹配（页眉）
            valid_matches = [m for m in matches if '续' not in m.group(0)]
            if valid_matches:
                return start_pos + valid_matches[0].start()
        
        return None
    
    def _find_canonical_name(self, raw_title: str) -> Optional[str]:
        """根据标题映射到标准章节名"""
        # 去掉"第X节"前缀
        cleaned = re.sub(r'第[一二三四五六七八九十]+节\s+', '', raw_title)
        
        for canonical_name, info in self.SECTION_DEFINITIONS.items():
            if canonical_name in cleaned or cleaned in canonical_name:
                return canonical_name
            for alias in info["aliases"]:
                if alias in cleaned or cleaned in alias:
                    return canonical_name
        return None
    
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
        logger.info("✅ 无损验证通过")
    
    def run(self, cleaned_text: str = None) -> dict:
        """执行 L1 粗切，可选传入已清洗的文本"""
        if cleaned_text is None:
            text = self.input_path.read_text(encoding="utf-8", errors="ignore")
        else:
            text = cleaned_text
        
        text_len = len(text)
        
        logger.info(f"📄 文档总长度: {text_len:,} 字符")
        
        # 1. 正则预筛选候选点
        candidates = self._find_candidates_with_regex(text)
        
        # 2. 合并为分析区域
        regions = self._merge_candidates_to_regions(candidates, text_len)
        
        # 3. LLM 精确认证
        boundaries = self._analyze_regions_with_llm(text, regions)
        logger.info(f"🎯 LLM 识别到 {len(boundaries)} 个边界")
        
        # 4. 合并近邻边界
        boundaries = self._merge_nearby_boundaries(boundaries)
        logger.info(f"🔄 去重后: {len(boundaries)} 个边界")
        
        # 5. 拆分合并标题
        boundaries = self._split_merged_titles(boundaries, text)
        
        # 6. 补充缺失章节
        boundaries = self._fill_missing_sections(boundaries, text)
        logger.info(f"🔧 补充后: {len(boundaries)} 个边界")
        
        # 7. 构建 blocks
        blocks_dir = self.output_dir / "blocks"
        blocks_dir.mkdir(exist_ok=True)
        blocks = []
        
        first_pos = boundaries[0]["pos"] if boundaries else text_len
        if first_pos > 0:
            blocks.append(self._make_block(text, "000", 0, first_pos, "封面与目录", "_PREAMBLE", blocks_dir))
        
        for i, boundary in enumerate(boundaries):
            start = boundary["pos"]
            end = boundaries[i + 1]["pos"] if i + 1 < len(boundaries) else text_len
            seq_id = f"{i + 1:03d}"
            blocks.append(
                self._make_block(text, seq_id, start, end, boundary["raw_title"], boundary["canonical_name"], blocks_dir))
        
        self._verify_lossless(text, blocks)
        
        manifest = {
            "doc_id": self.doc_id,
            "total_chars": text_len,
            "block_count": len(blocks),
            "blocks": [asdict(b) for b in blocks]
        }
        return manifest


# ==========================================
# 4. 阶段 3: L2 财务细切 (正则与先验知识探针)
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
            (r"(?:^|\n)\s*审计报告\s*\n", "审计报告"),
            (r"(?:^|\n)\s*合并资产负债表\s*\n", "合并资产负债表"),
            (r"(?:^|\n)\s*母公司资产负债表\s*\n", "母公司资产负债表"),
            (r"(?:^|\n)\s*合并利润表\s*\n", "合并利润表"),
            (r"(?:^|\n)\s*母公司利润表\s*\n", "母公司利润表"),
            (r"(?:^|\n)\s*合并现金流量表\s*\n", "合并现金流量表"),
            (r"(?:^|\n)\s*母公司现金流量表\s*\n", "母公司现金流量表"),
            (r"(?:^|\n)\s*合并所有者权益变动表\s*\n", "合并所有者权益变动表"),
            (r"(?:^|\n)\s*母公司所有者权益变动表\s*\n", "母公司所有者权益变动表"),
            (r"(?:^|\n)\s*财务报表附注\s*\n", "财务报表附注"),
        ]
        for pattern, name in hard_patterns:
            for match in re.finditer(pattern, text):
                anchors.append((match.start(), name))

        # 附注细分：识别关键附注（如存货、收入、关联交易）
        notes_pattern = r"(?:^|\n)\s*([一二三四五六七八九十]+、\s*[^\n]{2,30})\s*\n"
        for match in re.finditer(notes_pattern, text):
            title = match.group(1).strip()
            if any(k in title for k in ["存货", "收入", "关联交易", "应收", "应付", "金融工具", "公允价值"]):
                anchors.append((match.start(), f"附注_{title}"))

        anchors = sorted(set(anchors), key=lambda x: x[0])

        if not anchors:
            return [self._make_sub_block(text, parent_seq, parent_global_start, 0, len(text), "财务报告_未细分")]

        sub_blocks = []
        prev_end = 0
        for idx, (pos, name) in enumerate(anchors):
            if pos > prev_end:
                sub_blocks.append(self._make_sub_block(text, parent_seq, parent_global_start, prev_end, pos, name))
            prev_end = pos
        sub_blocks.append(self._make_sub_block(text, parent_seq, parent_global_start, prev_end, len(text), "财务报告_尾部"))

        return sub_blocks

    def _make_sub_block(self, text, parent_seq, parent_global_start, start, end, title):
        content = text[start:end]
        seq_str = f"{parent_seq}.{len([b for b in self.manifest['blocks'] if b['seq_id'].startswith(parent_seq)])}"
        tier = "HIGH" if "附注" in title else "MEDIUM"
        safe_name = title.replace(" ", "_").replace("/", "_")
        file_name = f"{seq_str}_{safe_name}.txt"
        file_path = self.blocks_dir / file_name
        file_path.write_text(content, encoding="utf-8")

        return asdict(
            TextBlock(seq_str, parent_global_start + start, parent_global_start + end, len(content), title, safe_name,
                      tier, f"blocks/{file_name}"))


# ==========================================
# 5. 顶层流水线封装 (Facade)
# ==========================================
class AShareReportPipeline:
    def __init__(self, input_path: str, output_dir: str, skip_summary: bool = True):
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.doc_id = self.input_path.stem
        self.cleaned_text = None
        self.manifest = None
        self.skip_summary = skip_summary
        self.detector = ReportTypeDetector(
            skip_log_path=self.output_dir / "skipped_summaries.json"
        )

    def execute_stage1(self) -> 'AShareReportPipeline':
        """执行 Stage 1: 文本清洗"""
        logger.info("----------------------------------------")
        logger.info("⚡ [Stage 1] 执行文本清洗...")
        
        text = self.input_path.read_text(encoding="utf-8", errors="ignore")
        
        # 摘要检测：如果是年度报告摘要，直接跳过
        if self.skip_summary:
            detection = self.detector.detect_from_text(text, doc_id=self.doc_id)
            if detection.is_summary:
                self.detector.record_skip(detection, self.input_path)
                raise ValueError(
                    f"🚫 检测到年度报告摘要，已跳过处理: {self.input_path.name} "
                    f"(置信度: {detection.confidence}, 原因: {'; '.join(detection.reasons)})"
                )
        
        cleaner = Stage1TextCleaner(output_dir=self.output_dir)
        self.cleaned_text, clean_stats = cleaner.clean(text, self.doc_id)
        
        # 保存清洗后的文本
        cleaned_path = self.output_dir / f"{self.doc_id}_cleaned.txt"
        cleaned_path.write_text(self.cleaned_text, encoding="utf-8")
        logger.info(f"📝 清洗后文本已保存: {cleaned_path}")
        
        logger.info(f"✅ Stage 1 完成: 原始 {clean_stats['original_chars']:,} 字符 -> 清洗后 {clean_stats['cleaned_chars']:,} 字符")
        return self

    def execute_stage2(self, model_name: str = "qwen2.5:7b-instruct-q8_0-json") -> 'AShareReportPipeline':
        """执行 Stage 2: L1 粗切"""
        logger.info("----------------------------------------")
        logger.info("⚡ [Stage 2] 执行 L1 章节粗切...")
        
        if self.cleaned_text is None:
            raise ValueError("请先执行 Stage 1 清洗")
        
        self.manifest = L1CoarseSplitter(self.input_path, self.output_dir, model_name).run(self.cleaned_text)
        
        # 保存 manifest
        manifest_path = self.output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"✅ Stage 2 完成: 切分为 {self.manifest['block_count']} 个 blocks")
        return self

    def execute_stage3(self) -> 'AShareReportPipeline':
        """执行 Stage 3: L2 财务细切"""
        logger.info("----------------------------------------")
        logger.info("⚡ [Stage 3] 执行 L2 财务报表与高危附注深切...")
        
        if self.manifest is None:
            raise ValueError("请先执行 Stage 2 粗切")
        
        self.manifest = L2FinancialSplitter(self.output_dir, self.manifest).run()
        
        # 更新 manifest
        manifest_path = self.output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"✅ Stage 3 完成: 细切为 {self.manifest['block_count']} 个 blocks")
        return self

    def run(self, model_name: str = "qwen2.5:7b-instruct-q8_0-json") -> dict:
        """执行完整流水线"""
        return self.execute_stage1().execute_stage2(model_name).execute_stage3().manifest


# ==========================================
# 6. CLI 入口
# ==========================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="A-Share Annual Report ELT Pipeline")
    parser.add_argument("txt", help="Input TXT file path")
    parser.add_argument("--out", default="output", help="Output directory")
    parser.add_argument("--model", default="qwen2.5:7b-instruct-q8_0-json", help="Ollama model name")
    args = parser.parse_args()

    pipeline = AShareReportPipeline(args.txt, args.out)
    pipeline.execute_stage1().execute_stage2(model_name=args.model).execute_stage3()
    logger.info("✅ 流水线完成！Manifest 索引表已写入: %s", pipeline.output_dir / "manifest.json")
