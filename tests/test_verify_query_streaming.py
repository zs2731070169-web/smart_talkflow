import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

from engine.client.base_client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from engine.client.messages import ConversationMessage, TextBlock, ToolUseBlock
from engine.query import Query
from engine.stream_event import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    ToolProgress,
)
from orchestrator.base import WorkflowResult
from orchestrator.workflow_engine import StepResult
from runtime.context import ModelContext, OperatorContext, RequestContext


class FakeApiClient:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_message(self, request) -> AsyncGenerator[object, None]:
        self.calls += 1
        if self.calls == 1:
            yield ApiTextDeltaEvent(text="好的，正在处理。")
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="tool_use_1",
                            name="meeting_room_booking",
                            input={
                                "room_id": 1,
                                "meeting_title": "周会",
                                "meeting_start_time": "2026-07-01 10:00:00",
                                "meeting_end_time": "2026-07-01 11:00:00",
                                "moderator_id": 10,
                                "use_status": 1,
                            },
                        )
                    ],
                )
            )
            return
        yield ApiTextDeltaEvent(text="已为您预订周会会议室，预订号 123。")
        yield ApiMessageCompleteEvent(message=ConversationMessage(role="assistant", content=[]))


class _Dispatcher:
    async def execute(self, workflow, inputs):
        yield StepResult(ok=True, data=123, name="提交预订", step_id=1, is_compensation=False)
        yield StepResult(ok=True, data=True, name="审批通过", step_id=2, is_compensation=False)
        yield WorkflowResult(
            output="已为您预订会议室:周会(预订号 123)",
            metadata={
                "steps": [
                    {"name": "提交预订", "ok": True, "step_id": 1, "is_compensation": False},
                    {"name": "审批通过", "ok": True, "step_id": 2, "is_compensation": False},
                ]
            },
        )


async def main() -> None:
    workflow = MagicMock()
    workflow.input_model.model_validate.side_effect = lambda data: data

    registry = MagicMock()
    registry.to_api_schema.return_value = [{"name": "meeting_room_booking"}]
    registry.get_workflow.return_value = workflow

    query = Query(registry, _Dispatcher())
    context = RequestContext(
        operator=OperatorContext(user_id="u1"),
        intent_model=ModelContext(model="intent-model", max_tokens=1024),
        reply_model=ModelContext(model="reply-model", max_tokens=1024),
        api_client=FakeApiClient(),
        system_prompt="test-system",
        messages=[ConversationMessage(role="user", content=[TextBlock(text="帮我订会议室")])],
        workflow_registry=registry,
        trace_id="trace-1",
    )

    events = []
    async for event in query.run(context):
        events.append(event)

    print("事件序列:")
    for idx, event in enumerate(events, 1):
        print(
            idx,
            type(event).__name__,
            getattr(event, "tool_name", ""),
            getattr(event, "step_name", ""),
            getattr(event, "text", ""),
        )

    assert isinstance(events[0], AssistantTextDelta)
    assert isinstance(events[1], ToolExecutionStarted)
    assert isinstance(events[2], ToolProgress) and events[2].step_name == "提交预订"
    assert isinstance(events[3], ToolProgress) and events[3].step_name == "审批通过"
    assert isinstance(events[4], ToolExecutionCompleted)
    assert isinstance(events[5], AssistantTextDelta)
    assert isinstance(events[6], AssistantTurnComplete)
    print("\n✅ Query.run 流式事件顺序验证通过")


if __name__ == "__main__":
    asyncio.run(main())
