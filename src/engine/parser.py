from collections.abc import AsyncGenerator

from pydantic import BaseModel, ValidationError

from engine.client.messages import ConversationMessage
from engine.stream_event import StreamEvent
from orchestrator.base import BaseWorkflow, WorkflowRegistry
from runtime.context import RequestContext


class IntentParser:
    """用户意图解析, 参数提取, 工具/工作流调用决策, 反馈执行结果。

    职责边界:LLM 输出 → 路由校验(_route)+ 参数校验(_validate)→ 调 dispatcher.execute。
    LLM 幻觉(工具名 / 参数格式错)在本层消化(内部重试,用户无感);参数缺则反问用户。
    执行编排(认证 / 权限 / 幂等 / 执行 / 状态)下沉到 dispatcher,本层只管「解析 + 校验 + 调度」。
    """

    def __init__(self, registry: WorkflowRegistry, dispatcher) -> None:
        self._registry = registry
        self._dispatcher = dispatcher

    def _route(self, name: str) -> BaseWorkflow | None:
        """路由:工具名 → workflow。不存在返回 None(由 run 内部带可用工具列表重试 LLM 消化幻觉)。"""
        return self._registry.get_workflow(name)

    def _validate(self, arguments: dict, workflow: BaseWorkflow) -> tuple[BaseModel | None, str | None]:
        """参数校验:arguments → inputs。校验失败返回 (None, 错误信息)(反问用户 / 重试 LLM)。"""
        try:
            return workflow.input_model.model_validate(arguments), None
        except ValidationError as e:
            return None, str(e)

    async def run(
        self, context: RequestContext, messages: list[ConversationMessage]
    ) -> AsyncGenerator[StreamEvent, None]:
        """获取用户查询, 执行解析。

        链路:LLM 解析 → name+arguments → _route(工具名校验)→ _validate(参数校验)
        → dispatcher.execute(workflow, inputs) → 结果/失败流式反馈。

        - LLM 解析(格式/工具名错 → IntentParser 内部重试消化,用户无感)
        - 参数缺 → 反问用户(合理)
        - 全 OK → 执行 → 结果 / 业务失败反馈

        TODO: LLM 调用 + 意图/参数提取(function calling)尚未实现,以下链路待接通。
        """
        # TODO: 1. 调 LLM 解析 messages → (name, arguments)(function calling;格式/工具名错带反馈重试)
        # TODO: 2. workflow = self._route(name);None → 带可用工具列表重试 LLM
        # TODO: 3. inputs, err = self._validate(arguments, workflow);失败 → 反问用户 / 重试 LLM
        # TODO: 4. result = await self._dispatcher.execute(workflow, inputs, context)
        # TODO: 5. 把 result 流式产出为 StreamEvent(text / tool_started / tool_completed)

