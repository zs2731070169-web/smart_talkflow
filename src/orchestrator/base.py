"""工作流抽象与注册器。

步骤执行引擎(``StepContext`` / ``WorkflowStep`` / ``DoneStep`` / ``StepMeta`` /
:func:`run_steps`)见 :mod:`orchestrator.workflow_engine`;:meth:`BaseWorkflow.execute`
委托它驱动顺序执行 + 留痕 + 失败逆序补偿。本模块只定义「workflow 是什么」
(抽象基类 + 注册器 + 上下文 / 结果 DTO)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from runtime.context import get_process_id

if TYPE_CHECKING:
    from orchestrator.workflow_engine import WorkflowStep
    from orchestrator.workflow_engine import run_steps


class WorkflowExecutionContext(BaseModel):
    """执行工具时使用的上下文"""

    metadata: dict[str, Any] = field(default_factory=dict)


class WorkflowResult(BaseModel):
    """工作流执行结果"""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseWorkflow(ABC):
    """工作流基类(步骤式编排):子类声明 steps,execute 委托 workflow_engine 驱动。"""

    name: str
    description: str
    input_model: type[BaseModel]

    @abstractmethod
    def steps(self, arguments: BaseModel, context: WorkflowExecutionContext) -> list[WorkflowStep]:
        """声明工作流的步骤序列(按 step_no 顺序执行)

        :param arguments: 业务参数
        :param context: 外部传入的执行上下文
        """

    @abstractmethod
    def business_key(self, arguments: BaseModel) -> str | None:
        """从入参提取业务唯一键(供流程级幂等校验使用)。"""

    async def execute(
            self, arguments: BaseModel, context: WorkflowExecutionContext
    ) -> WorkflowResult:
        """通用驱动:委托 workflow_engine 顺序执行声明的步骤,失败逆序补偿"""
        from orchestrator.workflow_engine import run_steps
        return await run_steps(get_process_id(), self.steps(arguments, context))

    def to_api_schema(self) -> dict[str, Any]:
        """将工作流定义结构序列化为API格式."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


class WorkflowRegistry:
    """工作流注册器."""

    def __init__(self) -> None:
        self._workflows: dict[str, BaseWorkflow] = {}

    def register(self, workflow: BaseWorkflow) -> None:
        """工作流实例注册."""
        self._workflows[workflow.name] = workflow

    def list_workflows(self) -> list[BaseWorkflow]:
        """返回所有注册的工作流."""
        return list(self._workflows.values())

    def get_workflow(self, name: str) -> BaseWorkflow:
        return self._workflows.get(name)

    def to_api_schema(self) -> list[dict[str, Any]]:
        """把所有的工作流定义序列化为API格式."""
        return [tool.to_api_schema() for tool in self._workflows.values()]
