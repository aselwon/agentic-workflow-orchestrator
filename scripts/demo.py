"""Live HTTP acceptance demo: python scripts/demo.py [--base-url http://localhost:8088]."""

import argparse
import time

import httpx


def demo(base_url):
    with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
        response = client.post("/api/runs", json={"query": "delayed"})
        response.raise_for_status()
        run_id = response.json()["id"]
        approved = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            response = client.get(f"/api/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            if run["status"] == "waiting_human" and not approved:
                print(f"HITL pause: {run['pending']['call']['arguments']['text']}")
                response = client.post(
                    f"/api/runs/{run_id}/decision",
                    json={"approval_id": run["pending"]["id"], "decision": "approve"},
                )
                response.raise_for_status()
                approved = True
            elif run["status"] == "succeeded":
                pauses = [e for e in run["events"] if e["kind"] == "approval_required"]
                assert approved and len(pauses) == 1, "Expected exactly one HITL pause"
                assert run["steps"] == 4 and len(run["memory"]) == 4
                assert sum(e["kind"] == "action" for e in run["events"]) == 4
                assert sum(e["kind"] == "observation" for e in run["events"]) == 4
                print(
                    f"PASS: run {run_id}, 4 tools, 1 HITL pause, {len(run['events'])} audit events"
                )
                print(f"UI: {base_url}/runs/{run_id}")
                return
            elif run["status"] == "failed":
                raise RuntimeError(run["error"])
            time.sleep(0.2)
        raise TimeoutError(f"Demo did not finish within 60 seconds: {run_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8088")
    demo(parser.parse_args().base_url)
