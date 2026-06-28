from orchestrator.base import WorkflowRegistry
from orchestrator.dispatcher import WorkflowDispatcher
from runtime.context import RequestContext


class Query:

    def __init__(self, registry: WorkflowRegistry, dispatcher: WorkflowDispatcher):
        pass

    def run(self, context: RequestContext):
        pass
