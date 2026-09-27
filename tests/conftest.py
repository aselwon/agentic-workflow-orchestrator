import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.mock_orders import ORDERS


@pytest.fixture
def application(tmp_path):
    # TEST_DATABASE_URL enables the same suite against real PostgreSQL.
    settings = Settings(
        database_url=os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db"),
        worker_enabled=False,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=ORDERS[request.url.path.rsplit("/", 1)[-1]])
    )
    return create_app(settings, transport)


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture
def runner(application, client):
    class Runner:
        def start(self, query="delayed"):
            response = client.post("/api/runs", json={"query": query})
            assert response.status_code == 201
            return response.json()["id"]

        def tick(self, run_id, count=1):
            for _ in range(count):
                application.state.orchestrator.tick(run_id)
            return client.get(f"/api/runs/{run_id}").json()

        def wait(self, run_id):
            return self.tick(run_id, 4)

        def decide(self, run_id, decision="approve", approval_id=None):
            if approval_id is None:
                approval_id = client.get(f"/api/runs/{run_id}").json()["pending"]["id"]
            return client.post(
                f"/api/runs/{run_id}/decision",
                json={"approval_id": approval_id, "decision": decision},
            )

    return Runner()
