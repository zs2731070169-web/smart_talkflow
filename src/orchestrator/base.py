"""工作流抽象与注册器"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from dataclasses import field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from orchestrator.workflow_engine import Step


class WorkflowResult(BaseModel):
    """工作流执行结果"""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseWorkflow(ABC):
    """工作流基类(generator 步骤编排):子类实现 create generator,execute 委托 drive 驱动。"""

    name: str
    description: str
    input_model: type[BaseModel]

    @abstractmethod
    def create(self, arguments: BaseModel) -> Generator[Step, Any, str]:
        """声明工作流:yield step(...) 声明步骤,return 最终文案, 补偿写在 ``except Compensate`` 分支"""

    @abstractmethod
    def business_key(self, arguments: BaseModel) -> str | None:
        """从入参提取业务唯一键(供流程级幂等校验使用)。"""

    async def execute(self, arguments: BaseModel, process_id: int | None) -> WorkflowResult:
        """通用驱动:委托 workflow_engine.drive 驱动"""
        from orchestrator.workflow_engine import ProcessContext, drive

        return await drive(self, arguments, ProcessContext(process_id=process_id))

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

    def register_workflows(self, workflows: list[BaseWorkflow]) -> None:
        """多个工作流实例注册"""
        for workflow in workflows:
            self.register(workflow)

    def list_workflows(self) -> list[BaseWorkflow]:
        """返回所有注册的工作流."""
        return list(self._workflows.values())

    def get_workflow(self, name: str) -> BaseWorkflow:
        return self._workflows.get(name)

    def to_api_schema(self) -> list[dict[str, Any]]:
        """把所有的工作流定义序列化为API格式."""
        return [tool.to_api_schema() for tool in self._workflows.values()]
