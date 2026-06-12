#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用 OpenClaw agent 分析已分割的财报文本。

设计思路：
- 客户端只告诉 agent 输入目录路径和读取规则
- agent 自己调用 read_file 工具读取相关章节文件
- agent 返回 JSON，客户端保存到输出目录

WSL 路径映射：
- Windows: D:\\WSL\\Files\\FinanceReportAnalysisPipeline\\output\\full_split_v3\\
- WSL:     /mnt/d/FinanceReportAnalysisPipeline/output/full_split_v3/
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from openclaw_client_async import OpenClawAsyncClient


# ───────────────────────────────────────────────
# 配置
# ───────────────────────────────────────────────

# 本地（Windows）分割结果目录
LOCAL_SPLIT_DIR = Path("output/full_split_v3")

# OpenClaw Gateway 在 WSL 中看到的分割结果目录
WSL_SPLIT_DIR = "/mnt/d/FinanceReportAnalysisPipeline/output/full_split_v3"

# OpenClaw Gateway host（WSL 中访问 Windows host 需用 10.255.255.254 等实际 IP）
OPENCLAW_HOST = os.environ.get("OPENCLAW_HOST", "127.0.0.1")
OPENCLAW_PORT = int(os.environ.get("OPENCLAW_PORT", "18789"))

# 本地输出目录
LOCAL_OUTPUT_DIR = Path("output/analysis_v3")

# Prompt 文件
PROMPT_PATH = Path("prompt.md")

# 并发数：根据 Gateway QPS 和显存调整
CONCURRENCY = 3

# 每处理 N 个保存一次进度
BATCH_SIZE = 10

# Agent 调用超时（秒）
AGENT_TIMEOUT = 300

# 失败重试次数
MAX_RETRIES = 3

# Demo token（仅本地测试使用；生产环境请通过 OPENCLAW_TOKEN 环境变量传入）
_DEMO_TOKEN = "d6b089820b17a7e722dd4f4a07a538d9d5b8680e8051a01e"


# ───────────────────────────────────────────────
# 工具函数
# ───────────────────────────────────────────────

def load_prompt() -> str:
    """加载 prompt.md"""
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"找不到 prompt 文件: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def parse_doc_id(doc_id: str) -> tuple:
    """从 doc_id 解析股票代码和公司名。

    格式示例：000004_ST国华_2025
    """
    parts = doc_id.split("_")
    if len(parts) >= 3:
        stock_code = parts[0]
        company_name = "_".join(parts[1:-1])
        year = parts[-1]
    elif len(parts) == 2:
        stock_code = parts[0]
        company_name = parts[1]
        year = ""
    else:
        stock_code = doc_id
        company_name = ""
        year = ""
    return stock_code, company_name, year


def build_message(doc_id: str, wsl_doc_dir: str, output_path: str) -> str:
    """构造给 agent 的 message。

    关键：让 agent 知道目录路径、文件命名规则、该读哪些章节。
    """
    stock_code, company_name, year = parse_doc_id(doc_id)

    return f"""请分析以下已分割的 A 股年报文本，并按 instructions 抽取结构化 JSON。

## 任务上下文
- doc_id: {doc_id}
- 公司代码: {stock_code}
- 公司名称: {company_name}
- 报告年份: {year}

## 输入目录
{wsl_doc_dir}

该目录下包含已按章节切分好的文本文件，文件名格式为「编号_章节名.txt」，例如：
- 010_前言及重要提示.txt
- 030_公司简介和主要财务指标.txt
- 040_管理层讨论与分析.txt
- 050_公司治理、环境和社会.txt
- 060_重要事项.txt
- 070_股份变动及股东情况.txt
- 080_债券相关情况.txt
- 090_财务报告.txt
- 110_关联方及关联交易.txt
- 120_重要交易和事项.txt

## 读取要求
请按编号顺序读取以下关键章节（如目录中不存在某章节则跳过），拼接文本后进行分析：

必读章节（覆盖全部抽取目标）：
1. 040_管理层讨论与分析.txt
2. 050_公司治理、环境和社会.txt
3. 070_股份变动及股东情况.txt
4. 090_财务报告.txt
5. 110_关联方及关联交易.txt

可选补充（当必读章节信息不足时）：
6. 030_公司简介和主要财务指标.txt
7. 120_重要交易和事项.txt

不需要读取的章节：
- 010_前言及重要提示.txt（风险提示，与抽取目标无关）
- 020_重要提示、目录和释义.txt（目录）
- 080_债券相关情况.txt（除非公司有债券业务）

## 输出要求
- 请直接返回严格合法的 JSON 文本，不要保存文件。
- 不要 Markdown 代码块（不要 ```json），不要任何解释、总结或注释。
- 严格遵循 instructions 中的 schema 和抽取规则。
- 未披露字段填 null 或空字符串，禁止编造任何文本中未出现的信息。
- 客户端会从你的返回文本中解析 JSON 并自行保存，你无需关心文件路径 {output_path}。
"""


def extract_json(text: str) -> dict:
    """从 agent 返回的文本中提取 JSON。

    处理常见污染：markdown 代码块、前后解释文字、截断等。
    """
    if not text or not text.strip():
        raise ValueError("Empty response text")

    text = text.strip()

    # 去掉 markdown 代码块
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    elif "```json" in text:
        text = text.split("```json")[-1].split("```")[0].strip()
    elif "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1].strip()

    # 找第一个 { 和最后一个 } 构成的对象
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in response")

    json_str = text[start : end + 1]
    return json.loads(json_str)


def get_output_text(response: dict) -> str:
    """从 OpenClaw Gateway 的 response JSON 中提取最终文本。"""
    text = ""
    outputs = response.get("output", []) if isinstance(response, dict) else []
    for item in outputs:
        if isinstance(item, dict):
            content = item.get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        text += c.get("text", "")
                    elif isinstance(c, str):
                        text += c
            elif isinstance(content, str):
                text += content
        elif isinstance(item, str):
            text += item
    return text


async def analyze_one(
    client: OpenClawAsyncClient,
    doc_id: str,
    wsl_doc_dir: str,
    output_path: Path,
    prompt_text: str,
    max_retries: int = MAX_RETRIES,
) -> Dict:
    print(f"Analysing {doc_id} in {wsl_doc_dir} -> {output_path}")

    """分析单个文档，支持重试。"""
    message = build_message(doc_id, wsl_doc_dir, str(output_path))

    for attempt in range(max_retries):
        try:
            response = await client.chat(
                message=message,
                instructions=prompt_text,
                # model="kimi/kimi-code",  # 如需指定模型可取消注释
            )

            text = get_output_text(response)
            if not text.strip():
                raise ValueError("Empty response text")

            data = extract_json(text)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            return {
                "doc_id": doc_id,
                "status": "success",
                "output_path": str(output_path),
            }

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            if attempt == max_retries - 1:
                return {
                    "doc_id": doc_id,
                    "status": "failed",
                    "error": error_msg,
                    "attempts": attempt + 1,
                }
            # 指数退避
            await asyncio.sleep(2 ** attempt)

    return {"doc_id": doc_id, "status": "failed", "error": "Max retries exceeded"}


def parse_args():
    parser = argparse.ArgumentParser(description="使用 OpenClaw agent 分析已分割的财报文本")
    parser.add_argument(
        "--doc-id",
        type=str,
        default=None,
        help="仅分析单个文档（例如 000001_平安银行_2025），用于调试",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="OpenClaw Gateway token；未提供时读取 OPENCLAW_TOKEN / OPENCLAW_GATEWAY_TOKEN 环境变量",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=CONCURRENCY,
        help=f"并发数（默认 {CONCURRENCY}）",
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default=str(LOCAL_SPLIT_DIR),
        help=f"本地分割结果目录（默认 {LOCAL_SPLIT_DIR}）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(LOCAL_OUTPUT_DIR),
        help=f"本地输出目录（默认 {LOCAL_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅处理前 N 个文档（用于小批量测试）",
    )
    return parser.parse_args()


async def run_single(client: OpenClawAsyncClient, doc_id: str, args) -> Dict:
    """单条调试模式。"""
    prompt_text = load_prompt()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    local_doc_dir = input_dir / doc_id
    if not local_doc_dir.exists():
        print(f"ERROR: 找不到目录 {local_doc_dir}")
        return {"doc_id": doc_id, "status": "failed", "error": "directory not found"}

    wsl_doc_dir = f"{WSL_SPLIT_DIR}/{doc_id}"
    output_path = output_dir / f"{doc_id}.json"

    print(f"[single] doc_id={doc_id}")
    print(f"[single] local_dir={local_doc_dir}")
    print(f"[single] wsl_dir={wsl_doc_dir}")
    print(f"[single] output_path={output_path}")

    result = await analyze_one(
        client=client,
        doc_id=doc_id,
        wsl_doc_dir=wsl_doc_dir,
        output_path=output_path,
        prompt_text=prompt_text,
        max_retries=1,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


async def run_batch(args) -> Dict:
    """批量处理模式。"""
    prompt_text = load_prompt()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists():
        print(f"ERROR: 输入目录不存在: {input_dir}")
        return {"total": 0, "success": 0, "failed": 0, "error": "input dir not found"}

    output_dir.mkdir(parents=True, exist_ok=True)

    doc_ids = sorted([
        d.name for d in input_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    ])
    # if args.limit:
    #     doc_ids = doc_ids[:args.limit]
    total = len(doc_ids)

    progress_path = output_dir / "_progress.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        processed = set(progress.get("processed", []))
        details = progress.get("details", [])
        print(f"analyze start: {total} docs, resume {len(processed)} already processed")
    else:
        processed = set()
        details = []
        print(f"analyze start: {total} docs")

    print('--------------------- doc ids ---------------------')
    print(doc_ids)
    print('------------------ processed ids ------------------')
    print(processed)
    print('---------------------------------------------------')

    start_time = time.time()

    async with OpenClawAsyncClient(
        token=args.token,
        timeout=AGENT_TIMEOUT,
    ) as client:

        semaphore = asyncio.Semaphore(args.concurrency)

        async def wrapped(doc_id: str):
            async with semaphore:
                if doc_id in processed:
                    print(f"doc_id {doc_id} is in processed.")
                    return None
                wsl_doc_dir = f"{WSL_SPLIT_DIR}/{doc_id}"
                output_path = output_dir / f"{doc_id}.json"
                return await analyze_one(
                    client=client,
                    doc_id=doc_id,
                    wsl_doc_dir=wsl_doc_dir,
                    output_path=output_path,
                    prompt_text=prompt_text,
                )

        tasks = [asyncio.create_task(wrapped(d)) for d in doc_ids]

        print(f"Task count: {len(tasks)}")

        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result is None:
                print('Result is None.')
                continue

            details.append(result)
            if result["status"] == "success":
                processed.add(result["doc_id"])
            completed += 1

            if args.limit:
                if completed >= args.limit:
                    print(f"Limit {args.limit} reached. Quit.")
                    break

            if completed % BATCH_SIZE == 0:
                progress_path.write_text(
                    json.dumps(
                        {"processed": sorted(processed), "details": details},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                elapsed = time.time() - start_time
                speed = completed / elapsed if elapsed > 0 else 0
                remaining = total - len(processed)
                eta_hours = remaining / speed / 3600 if speed > 0 else float("inf")
                print(
                    f"[{len(processed)}/{total}] success={len(processed)} "
                    f"failed={completed - len(processed)} "
                    f"speed={speed:.2f}d/s remaining≈{eta_hours:.1f}h"
                )

    progress_path.write_text(
        json.dumps(
            {"processed": sorted(processed), "details": details},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = {
        "total": total,
        "success": sum(1 for d in details if d["status"] == "success"),
        "failed": sum(1 for d in details if d["status"] == "failed"),
        "elapsed_seconds": round(time.time() - start_time, 2),
        "details": details,
    }
    (output_dir / "_batch_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"DONE: success={summary['success']} failed={summary['failed']} "
        f"elapsed={summary['elapsed_seconds']}s"
    )
    return summary


async def main():
    args = parse_args()

    token = args.token or os.environ.get("OPENCLAW_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if not token:
        token = _DEMO_TOKEN
        print("WARN: 未提供 token，使用内置 demo token（仅本地测试）")
    args.token = token

    if args.doc_id:
        async with OpenClawAsyncClient(
            token=args.token,
            timeout=AGENT_TIMEOUT,
            host=OPENCLAW_HOST,
            port=OPENCLAW_PORT,
        ) as client:
            await run_single(client, args.doc_id, args)
    else:
        await run_batch(args)


if __name__ == "__main__":
    asyncio.run(main())
