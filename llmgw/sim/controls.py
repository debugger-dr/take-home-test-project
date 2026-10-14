from collections import deque

from llmgw.types import Accepted, Dispatch, LLMRequest, Rejected, Usage


class PassThroughScheduler:
    """Negative control: dispatches everything immediately with no quota check and no fairness."""

    def __init__(self) -> None:
        self._queue: deque[LLMRequest] = deque()

    def submit(self, req: LLMRequest, now: float) -> Accepted | Rejected:
        self._queue.append(req)
        return Accepted(req.id, 0.0)

    def poll(self, now: float) -> list[Dispatch]:
        dispatches = [
            Dispatch(r.id, r.tenant, r.est_input_tokens + r.max_output_tokens, attempt=1)
            for r in self._queue
        ]
        self._queue.clear()
        return dispatches

    def on_complete(self, request_id: str, usage: Usage, now: float) -> None:
        pass

    def on_provider_rejected(self, request_id: str, retry_after: float, now: float) -> None:
        pass

    def next_wakeup(self, now: float) -> float | None:
        return None
