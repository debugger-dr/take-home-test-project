import pytest

from llmgw.types import Klass, LLMRequest


@pytest.fixture
def make_request():
    def factory(
        id="r", tenant="a", est_input=100, max_output=100, klass=Klass.P1, at=0.0
    ) -> LLMRequest:
        return LLMRequest(
            id=id, tenant=tenant, run=f"{tenant}-run", agent=f"{tenant}-agent", model="m",
            klass=klass, est_input_tokens=est_input, max_output_tokens=max_output,
            deadline=at + 100, submitted_at=at,
        )

    return factory
