import pytest


@pytest.fixture(autouse=True)
def default_service_token(monkeypatch):
	monkeypatch.setenv("MYAPP_AI_SERVICE_TOKEN", "0123456789abcdef0123456789abcdef")
