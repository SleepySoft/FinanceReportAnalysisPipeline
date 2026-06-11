#!/usr/bin/env python3
"""
OpenClaw Agent HTTP Client (异步版)
通过 Gateway HTTP API 与 agent 交互，等待返回结果。

依赖: pip install aiohttp

两种模式:
1. chat()      -> /v1/responses  对话式，agent 会思考、用工具、返回结果
2. tool()      -> /tools/invoke  直接调用单个工具，无 agent 思考过程

Gateway: http://127.0.0.1:18789
"""

import asyncio
import json
import os
import sys
from typing import Optional

import aiohttp


class OpenClawAsyncClient:
    """OpenClaw Gateway 异步 HTTP Client"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18789,
        token: Optional[str] = None,
        agent_id: str = "asuka",
        timeout: int = 120,
    ):
        self.base_url = f"http://{host}:{port}"
        self.agent_id = agent_id
        self.timeout = aiohttp.ClientTimeout(total=timeout)

        self.token = token or os.environ.get("OPENCLAW_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        if not self.token:
            raise ValueError(
                "需要提供 token: 传入 token=xxx 或设置环境变量 OPENCLAW_TOKEN"
            )

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ───────────────────────────────────────────────
    # 模式 1: 对话式 (/v1/responses)
    # ───────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        session_key: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = False,
        instructions: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> dict:
        """
        发送消息给 agent，等待返回结果。

        Args:
            message: 要发送的消息内容
            session_key: 会话 key，相同 key 会共享上下文
            model: 后端模型，如 "kimi/kimi-code"
            stream: 是否流式返回（SSE）
            instructions: 系统指令
            max_output_tokens: 最大输出 token 数

        Returns:
            完整的 response JSON
        """
        payload = {
            "model": model or "openclaw",
            "input": message,
        }

        if instructions:
            payload["instructions"] = instructions
        if max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
        if stream:
            payload["stream"] = True

        req_headers = dict(self.headers)
        req_headers["x-openclaw-agent-id"] = self.agent_id

        if session_key:
            req_headers["x-openclaw-session-key"] = session_key
        if model:
            req_headers["x-openclaw-model"] = model

        url = f"{self.base_url}/v1/responses"

        if stream:
            return await self._chat_stream(url, payload, req_headers)
        else:
            return await self._chat_sync(url, payload, req_headers)

    async def _chat_sync(self, url: str, payload: dict, headers: dict) -> dict:
        session = await self._get_session()
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _chat_stream(self, url: str, payload: dict, headers: dict) -> dict:
        session = await self._get_session()
        output_text = ""
        response_id = None
        status = None

        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()

            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line:
                    continue

                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")

                    if event_type == "response.created":
                        response_id = event.get("response", {}).get("id")
                        status = "created"

                    elif event_type == "response.output_text.delta":
                        output_text += event.get("delta", "")

                    elif event_type == "response.completed":
                        return event.get("response", {})

                    elif event_type == "response.failed":
                        raise RuntimeError(f"Agent response failed: {event}")

        return {
            "id": response_id,
            "status": status or "unknown",
            "output_text": output_text,
        }

    # ───────────────────────────────────────────────
    # 模式 2: 直接调用工具 (/tools/invoke)
    # ───────────────────────────────────────────────

    async def tool(
        self,
        tool_name: str,
        args: Optional[dict] = None,
        action: Optional[str] = None,
        session_key: Optional[str] = None,
    ) -> dict:
        """
        直接调用单个工具。

        Args:
            tool_name: 工具名，如 "web_search", "memory_search"
            args: 工具参数
            action: 某些工具需要 action 参数
            session_key: 目标会话 key

        Returns:
            { ok: bool, result: ... }
        """
        payload = {"tool": tool_name}

        if args:
            payload["args"] = args
        if action:
            payload["action"] = action
        if session_key:
            payload["sessionKey"] = session_key

        url = f"{self.base_url}/tools/invoke"
        session = await self._get_session()

        async with session.post(url, json=payload, headers=self.headers) as resp:
            if resp.status == 404:
                return {
                    "ok": False,
                    "error": {
                        "type": "tool_not_found",
                        "message": f"Tool '{tool_name}' not found or not allowed",
                    },
                }
            resp.raise_for_status()
            return await resp.json()

    # ───────────────────────────────────────────────
    # 快捷方法
    # ───────────────────────────────────────────────

    async def web_search(self, query: str, count: int = 5) -> dict:
        return await self.tool("web_search", args={"query": query, "count": count})

    async def memory_search(self, query: str, max_results: int = 5) -> dict:
        return await self.tool("memory_search", args={"query": query, "maxResults": max_results})

    async def read_file(self, path: str) -> dict:
        return await self.tool("read", args={"path": path})

    async def exec(self, command: str, timeout: Optional[int] = None) -> dict:
        args = {"command": command}
        if timeout:
            args["timeout"] = timeout
        return await self.tool("exec", args=args)


# ═══════════════════════════════════════════════════
# 使用示例
# ═══════════════════════════════════════════════════

async def demo_chat():
    """示例：对话式调用"""
    async with OpenClawAsyncClient(
        token="d6b089820b17a7e722dd4f4a07a538d9d5b8680e8051a01e",
        agent_id="asuka",
    ) as client:

        # 简单对话
        print("=== 简单对话 ===")
        result = await client.chat("你好，今天星期几？")
        text = result.get("output", [{}])[-1].get("content", [{}])[0].get("text", "")
        print(text)

        # 上下文对话
        print("\n=== 上下文对话 ===")
        await client.chat("记住我的名字是张三", session_key="demo_session")
        result = await client.chat("我叫什么名字？", session_key="demo_session")
        text = result.get("output", [{}])[-1].get("content", [{}])[0].get("text", "")
        print(text)

        # 流式输出
        print("\n=== 流式输出 ===")
        result = await client.chat("讲一个短笑话", stream=True)
        text = result.get("output", [{}])[-1].get("content", [{}])[0].get("text", "")
        print(text)


async def demo_tool():
    """示例：直接调用工具"""
    async with OpenClawAsyncClient(
        token="d6b089820b17a7e722dd4f4a07a538d9d5b8680e8051a01e",
    ) as client:

        # 搜索
        print("=== 搜索 ===")
        result = await client.web_search("Python asyncio 教程", count=3)
        print(json.dumps(result, ensure_ascii=False, indent=2)[:500])

        # 读取文件
        print("\n=== 读取文件 ===")
        result = await client.read_file("/home/sleepy/.openclaw/workspace-asuka/SOUL.md")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:500])


async def demo_batch():
    """示例：并发批量调用"""
    async with OpenClawAsyncClient(
        token="d6b089820b17a7e722dd4f4a07a538d9d5b8680e8051a01e",
    ) as client:

        # 并发搜索多个关键词
        queries = ["Python 异步", "Go 并发", "Rust 所有权"]
        tasks = [client.web_search(q, count=3) for q in queries]
        results = await asyncio.gather(*tasks)

        for q, r in zip(queries, results):
            ok = r.get("ok", False)
            print(f"{q}: {'成功' if ok else '失败'}")


async def demo_advanced():
    """高级示例"""
    async with OpenClawAsyncClient(
        token="d6b089820b17a7e722dd4f4a07a538d9d5b8680e8051a01e",
    ) as client:

        # 指定模型 + 限制输出
        result = await client.chat(
            "解释量子计算",
            model="kimi/kimi-code",
            max_output_tokens=500,
            instructions="用中文回答，尽量简洁。",
        )
        text = result.get("output", [{}])[-1].get("content", [{}])[0].get("text", "")
        print(text)


if __name__ == "__main__":
    # 检查环境变量
    if not os.environ.get("OPENCLAW_TOKEN"):
        print("提示: 未设置 OPENCLAW_TOKEN 环境变量，代码中已硬编码 token")
        print("建议: export OPENCLAW_TOKEN='your_token_here'")
        print()

    print("=" * 60)
    print("OpenClaw Agent Async HTTP Client Demo")
    print("=" * 60)

    # 运行示例
    asyncio.run(demo_chat())
    # asyncio.run(demo_tool())
    # asyncio.run(demo_batch())
    # asyncio.run(demo_advanced())
