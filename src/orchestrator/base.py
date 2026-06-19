from abc import ABC, abstractmethod
from dataclasses import field
from typing import Any

from pydantic import BaseModel

from runtime.context import OperatorContext


class WorkflowExecutionContext(BaseModel):
    """执行工具时使用的上下文."""

    metadata: dict[str, Any] = field(default_factory=dict)


class WorkflowResult(BaseModel):
    """工作流执行结果."""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseWorkflow(ABC):
    """工作流基类."""

    name: str
    description: str
    input_model: type[BaseModel]
    # 允许触发本流程的角色; 默认空集表示不限制角色。
    allowed_roles: set[str] = set()

    def is_allowed(self, operator: OperatorContext) -> bool:
        """层 A:operator 是否有权触发本流程。

        :param operator: 当前请求操作人
        :return: allowed_roles 
        """
        if not self.allowed_roles:
            return True
        return bool(set(operator.roles) & self.allowed_roles)

    @abstractmethod
    def business_key(self, arguments: BaseModel) -> str | None:
        """从入参提取业务唯一键(供流程级幂等校验使用)。"""

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: WorkflowExecutionContext) -> WorkflowResult:
        """执行工作流接口."""

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
