"""对话编排器:意图理解/回复生成 + 校验 + 并发执行。

主链路:
- 调用 1(传 tools):生成 tool_use 决策
- 校验(get_workflow + model_validate)
- 并发执行(dispatcher.execute, fan-in 透传 ToolProgress)
- 调用 2(不传 tools):基于 tool_result 生成最终回复
"""

from __future__ import annotations

import asyncio
from typing import Any

from engine.client.base_client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiTextDeltaEvent,
    SupportsStreamingMessages,
)
from engine.client.messages import ConversationMessage, ToolResultBlock
from engine.stream_event import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    ToolProgress,
)
from infra.logger import setup_logging
from orchestrator.base import WorkflowRegistry, WorkflowResult
from orchestrator.dispatcher import WorkflowDispatcher
from runtime.context import ModelContext, RequestContext

logger = setup_logging(__name__)


async def _stream_chat(
    api_client: SupportsStreamingMessages,
    messages: list[ConversationMessage],
    system_prompt: str,
    model_context: ModelContext,
    tools: list[dict[str, Any]] | None = None,
):
    """通用 LLM 流式调用。

    入参通过 ``messages`` / ``tools`` 参数化:
    - 调用 1(意图理解):用户消息 + tools
    - 调用 2(回复生成):含 tool_result 的历史消息,不传 tools
    yield AssistantTextDelta(文本流) + 末尾 AssistantTurnComplete(本次调用完成)。
    """
    final_message: ConversationMessage | None = None
    logger.info("_stream_chat: start model=%s tools=%s messages=%d", model_context.model, bool(tools), len(messages))
    async for event in api_client.stream_message(
        request=ApiMessageRequest(
            model=model_context.model,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=model_context.max_tokens,
            tools=tools,
        )
    ):
        if isinstance(event, ApiTextDeltaEvent):
            yield AssistantTextDelta(text=event.text)
            continue
        if isinstance(event, ApiMessageCompleteEvent):
            final_message = event.message
    if final_message is None:
        raise RuntimeError("模型流式输出完成, 但是没有最终消息")
    yield AssistantTurnComplete(message=final_message)


def _serialize_result(workflow_name: str, workflow_result: WorkflowResult) -> str:
    """WorkflowResult → tool_result.content:结论 + 状态 + 过程概览(含幂等短路语义)。"""
    metadata = workflow_result.metadata or {}
    serialized_output = [f"[{workflow_name}] {'failed' if workflow_result.is_error else 'success'}"]

    # 幂等短路语义(让调用 2 的 LLM 知道这是历史结果复用,不是本次重新执行)
    if metadata.get("idempotent_hit"):
        serialized_output.append("source: 历史结果复用(本次未重新执行)")
        if metadata.get("status"):
            serialized_output.append(f"status: {metadata['status']}")
        if metadata.get("error"):
            serialized_output.append(f"error: {metadata['error']}")

    # 最终结论
    if workflow_result.output:
        serialized_output.append(workflow_result.output)

    # 补偿步骤概览
    if metadata.get("compensated"):
        serialized_output.append("(已补偿回滚)")

    # 正常步骤概览
    steps = metadata.get("steps") or []
    if steps:
        summary = ", ".join(f"{step.get('name', '?')} {'✗' if not step.get('ok', True) else '✓'}" for step in steps)
        serialized_output.append(f"steps: {summary}")

    return "\n".join(serialized_output)


async def _stream_process_step(workflow_queue: asyncio.Queue, total: int, tool_results: list[ToolResultBlock]):
    """消费并发执行队列:透传 ToolProgress,收集 WorkflowCompleted 对应的 tool_results。"""
    completed = 0
    while completed < total:
        status, tool_use, result = await workflow_queue.get()
        # 步级进度事件
        if status == "progress":
            yield ToolProgress(
                tool_name=tool_use.name,
                step_name=result.name,
                is_error=not result.ok,
                step_id=result.step_id,
                is_compensation=result.is_compensation,
                error=result.error,
            )
        # WorkflowCompleted 收集
        else:
            output = _serialize_result(tool_use.name, result)
            yield ToolExecutionCompleted(
                tool_name=tool_use.name,
                output=output,
                is_error=result.is_error,
                metadata=result.metadata,
            )
            tool_results.append(
                ToolResultBlock(
                    tool_use_id=tool_use.id,
                    content=output,
                    is_error=result.is_error,
                    result_metadata=dict(result.metadata or {}),
                )
            )
            completed += 1


class Query:
    """对话编排器:持有 registry/dispatcher,run 串联两次 _stream_chat + 校验 + 并发执行。"""

    def __init__(self, registry: WorkflowRegistry, dispatcher: WorkflowDispatcher) -> None:
        self._registry = registry
        self._dispatcher = dispatcher

    async def run(self, context: RequestContext):
        """对话编排:① 意图理解 ② 校验 + 并发执行 ③ 回复生成。"""
        messages = list(context.messages)

        # ① 意图理解(调用 1:传 tools)
        final_message: ConversationMessage | None = None
        async for event in _stream_chat(
            context.api_client,
            messages,
            context.intent_system_prompt,
            context.intent_model,
            tools=self._registry.to_api_schema(),
        ):
            if isinstance(event, AssistantTextDelta):
                yield event
                continue
            # 意图理解完成, 输出 AssistantTurnComplete
            final_message = event.message
            # 工具不存在, 纯聊天返回兜底
            if not final_message.tool_uses:
                yield event
                return
            # 模型返回的工作流调用
            messages.append(final_message)
            break

        # 获取工作流
        tool_uses = final_message.tool_uses

        # ② 校验(get_workflow + model_validate)
        validated_workflows, error_results = [], []
        for tool_use in tool_uses:
            # 校验工作流是否存在
            workflow = self._registry.get_workflow(tool_use.name)
            if workflow is None:
                error_results.append(
                    ToolResultBlock(
                        tool_use_id=tool_use.id,
                        content=f"未知工具: {tool_use.name}",
                        is_error=True,
                    )
                )
                continue
            # 校验参数是否正确
            try:
                inputs = workflow.input_model.model_validate(tool_use.input)
            except Exception as exc:
                error_results.append(
                    ToolResultBlock(
                        tool_use_id=tool_use.id,
                        content=f"无效参数: {exc}",
                        is_error=True,
                    )
                )
                continue
            # 追加校验通过的工作流
            validated_workflows.append((tool_use, workflow, inputs))

        # 流式打印开始执行工作流的消息
        logger.info("run: validated_workflows=%d error_results=%d", len(validated_workflows), len(error_results))
        for tool_use, _, _ in validated_workflows:
            logger.info("run: workflow=%s 通过校验,准备执行", tool_use.name)
            yield ToolExecutionStarted(tool_name=tool_use.name, tool_input=tool_use.input)

        # workflow 队列(并发 fan-in)
        workflow_queue: asyncio.Queue = asyncio.Queue()

        # 创建协程任务, 注册到事件循环
        tasks = [
            asyncio.create_task(self._run_workflows(tool_use, workflow, inputs, workflow_queue))
            for (tool_use, workflow, inputs) in validated_workflows
        ]

        # 消费 queue 流式输出步骤消息: ToolProgress yield; WorkflowCompleted 收集 tool_results
        tool_results: list[ToolResultBlock] = []
        async for event in _stream_process_step(workflow_queue, len(validated_workflows), tool_results):
            yield event

        # 等待全部任务并发执行完成
        await asyncio.gather(*tasks, return_exceptions=True)

        # 构建 user 消息(成功 tool_result + 校验失败 error_result), 输入给生成模型
        messages.append(ConversationMessage(role="user", content=tool_results + error_results))

        # ③ 回复生成(不传 tools,进行最终恢复)
        async for event in _stream_chat(
            context.api_client, messages, context.reply_system_prompt, context.reply_model, tools=None
        ):
            yield event

    async def _run_workflows(self, tool_use, workflow, inputs, queue: asyncio.Queue):
        """并发执行单个 workflow:消费 dispatcher.execute,步骤进度 + 整体结果汇入队列。"""
        async for result in self._dispatcher.execute(workflow, inputs):
            if isinstance(result, WorkflowResult):
                await queue.put(("completed", tool_use, result))  # 整体结果(完成标记)
            else:
                await queue.put(("progress", tool_use, result))  # 每步输出
