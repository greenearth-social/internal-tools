import os
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

# Set required env vars before importing app
os.environ.setdefault("GE_DISCORD_APPLICATION_ID", "test_app_id")
os.environ.setdefault("GE_DISCORD_PUBLIC_KEY", "a" * 64)
os.environ.setdefault("GE_DISCORD_BOT_TOKEN", "Bot test_token")
os.environ.setdefault("GE_DISCORD_ONCALL_CHANNEL_ID", "test_channel")
os.environ.setdefault("GE_FIRESTORE_PROJECT_ID", "test-project")
os.environ.setdefault("GE_GITHUB_TOKEN", "test_github_token")
os.environ.setdefault("GE_ONCALL_RUNBOOKS_BRANCH", "main")

from app import app

@pytest.fixture()
def mock_db():
    return MagicMock()

@pytest.fixture()
def client(mock_db):
    app.state.db = mock_db
    return TestClient(app)
