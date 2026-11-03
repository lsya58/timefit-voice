import pytest

from timefit.llm import reset_model_state


@pytest.fixture(autouse=True)
def _fresh_model_state():
    reset_model_state()
    yield
    reset_model_state()
