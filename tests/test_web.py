"""The web app through its HTTP surface, with fake repos, a fake runner and a fake LLM."""

import json
import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from setuppilot.agent.driver import SetupAgent
from setuppilot.agent.graph import AgentDeps
from setuppilot.chat.bot import RepoChatbot, index_repo
from setuppilot.chat.index import InMemoryChunkIndex
from setuppilot.config import WebConfig, load_settings
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.runs import InMemoryRunStore
from setuppilot.db.stores import Stores
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.usage import InMemoryUsageStore
from setuppilot.web.app import WebServices, create_app
from setuppilot.web.runs import CommandTracker, RunManager
from tests.fakes import FakeEmbedder, FakeTransport, make_client, make_repo, reply
from tests.test_agent import AllInstalled, FakeRunner, fake_clone
from tests.test_config import REAL_CONFIG

SETTINGS = load_settings(REAL_CONFIG)
HOST = f"127.0.0.1:{SETTINGS.web.port}"
URL = "https://github.com/someone/demo"


@pytest.fixture
def web(tmp_path):
    source = make_repo(tmp_path / "source", {"requirements.txt": "", "LICENSE": "MIT License"})
    (tmp_path / "dest").mkdir()
    stores = Stores(
        InMemoryRunStore(), InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore(),
        InMemoryChunkIndex(), InMemorySaver(), None,
    )  # fmt: skip
    runner = FakeRunner()
    deps = AgentDeps(SETTINGS, stores.runs, None, fake_clone(source), AllInstalled(), runner)
    manager = RunManager(SetupAgent(deps, stores.checkpointer), stores.runs, CommandTracker())
    index_repo("someone/demo", URL, source, SETTINGS.chat, 2000, FakeEmbedder(), stores.chat_index)
    answer = json.dumps({"answer": "MIT.", "found": True, "sources": ["LICENSE"]})
    llm = make_client(FakeTransport(*[reply(answer)] * 5))
    services = WebServices(
        settings=SETTINGS,
        stores=stores,
        runs=manager,
        embedder=FakeEmbedder(),
        make_chatbot=lambda: RepoChatbot(SETTINGS.chat, stores.chat_index, FakeEmbedder(), llm),
        llm_problem=None,
        preflight=lambda folder: [{"name": "Folder", "is_ok": bool(folder), "fix": ""}],
    )
    app = create_app(services)
    client = app.test_client()
    page = client.get("/", headers={"Host": HOST}).get_data(as_text=True)
    token = page.split('name="csrf-token" content="')[1].split('"')[0]
    return client, token, tmp_path, stores, runner


def post(web, path, body, token=None, **headers):
    client, real_token = web[0], web[1]
    headers = {"Host": HOST, "X-CSRF-Token": token if token is not None else real_token} | headers
    return client.post(path, json=body, headers=headers)


def get(web, path):
    return web[0].get(path, headers={"Host": HOST})


def wait_for(web, run_id, done):
    for _ in range(200):
        view = get(web, f"/api/runs/{run_id}").get_json()
        if not view["is_busy"] and done(view):
            return view
        time.sleep(0.02)
    raise AssertionError(f"run never reached the expected state: {view}")


def answer(web, run_id, view, reply):
    return post(web, f"/api/runs/{run_id}/answer", reply | {"question_id": view["question_id"]})


def start(web):
    response = post(web, "/api/runs", {"repo_url": URL, "dest_dir": str(web[2] / "dest")})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["run_id"]


def test_full_flow_in_the_browser_ends_verified(web):
    run_id = start(web)
    for _ in range(2):
        view = wait_for(web, run_id, lambda v: v["question"] or v["report"])
        answer(web, run_id, view, {"action": "approve"})
    view = wait_for(web, run_id, lambda v: v["report"])
    assert view["is_verified"] and view["run"]["status"] == "succeeded"
    assert [s["name"] for s in view["steps"]][:3] == ["clone", "scan", "detect"]


def test_refresh_shows_the_same_waiting_question(web):
    run_id = start(web)
    first = wait_for(web, run_id, lambda v: v["question"])
    assert get(web, f"/runs/{run_id}").status_code == 200
    again = get(web, f"/api/runs/{run_id}").get_json()
    assert again["question"] == first["question"] and web[4].texts == []


def test_cancel_while_waiting_stops_the_run_and_refuses_answers(web):
    run_id = start(web)
    waiting = wait_for(web, run_id, lambda v: v["question"])
    post(web, f"/api/runs/{run_id}/cancel", {})
    view = get(web, f"/api/runs/{run_id}").get_json()
    assert view["run"]["status"] == "cancelled" and view["question"] is None
    assert answer(web, run_id, waiting, {"action": "approve"}).status_code == 400


def test_a_repeated_approve_cannot_approve_the_next_unseen_command(web):
    run_id = start(web)
    first = wait_for(web, run_id, lambda v: v["question"])
    assert answer(web, run_id, first, {"action": "approve"}).status_code == 200
    wait_for(web, run_id, lambda v: v["question"] and v["question_id"] != first["question_id"])
    assert answer(web, run_id, first, {"action": "approve"}).status_code == 400
    assert web[4].texts == ["uv venv .venv"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"repo_url": "https://evil.com/x/y", "dest_dir": "/tmp"}, "public GitHub repo URL"),
        ({"repo_url": URL, "dest_dir": "/no/such/folder"}, "does not exist"),
        ({"repo_url": URL}, "dest dir"),
        ({"repo_url": "x" * 400, "dest_dir": "/tmp"}, "repo url"),
    ],
)
def test_bad_setup_input_gets_a_friendly_error(web, body, message):
    response = post(web, "/api/runs", body)
    assert response.status_code == 400 and message in response.get_json()["error"]["message"]


@pytest.mark.parametrize(
    "reply_body", [{"action": "run-anything"}, {"choice": "1"}, {"action": "edit", "command": ""}]
)
def test_only_known_answers_are_accepted(web, reply_body):
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    assert answer(web, run_id, view, reply_body).status_code == 400


def test_post_without_the_page_token_is_refused(web):
    assert post(web, "/api/runs", {"repo_url": URL}, token="wrong").status_code == 403


def test_post_from_another_website_is_refused(web):
    response = post(web, "/api/runs", {"repo_url": URL}, Origin="https://evil.example")
    assert response.status_code == 403


def test_request_with_a_strange_host_is_refused(web):
    # DNS rebinding: an outside page reaches 127.0.0.1 under its own name.
    assert web[0].get("/", headers={"Host": "evil.example:5005"}).status_code == 400


def test_too_many_posts_are_slowed_down(web, monkeypatch):
    responses = [post(web, "/api/chat/ask", {}) for _ in range(SETTINGS.web.posts_per_minute + 1)]
    assert responses[-1].status_code == 429


def test_huge_request_is_refused(web):
    assert post(web, "/api/chat/ask", {"question": "x" * 20000}).status_code == 413


def test_pages_have_safety_headers(web):
    response = get(web, "/")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("path", ["/", "/runs", "/metrics", "/settings", "/chat"])
def test_every_page_opens(web, path):
    assert get(web, path).status_code == 200


def test_unknown_run_page_is_not_found(web):
    assert get(web, "/api/runs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_events_stream_ends_with_the_finished_view(web):
    run_id = start(web)
    wait_for(web, run_id, lambda v: v["question"])
    post(web, f"/api/runs/{run_id}/cancel", {})
    body = get(web, f"/api/runs/{run_id}/events").get_data(as_text=True)
    last = json.loads(body.strip().split("data: ")[-1])
    assert last["run"]["status"] == "cancelled"


def test_chat_answers_with_sources(web):
    response = post(
        web, "/api/chat/ask", {"repo": "someone/demo", "question": "Is it MIT License?"}
    )
    assert response.get_json()["sources"] == ["LICENSE"]


def test_preflight_lists_its_checks(web):
    response = web[0].get(
        "/api/preflight?folder=/tmp", headers={"Host": HOST, "X-CSRF-Token": web[1]}
    )
    assert response.get_json()["checks"][0]["is_ok"]


def test_preflight_needs_the_page_token_because_it_runs_programs(web):
    assert get(web, "/api/preflight?folder=/tmp").status_code == 403


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_web_server_can_never_listen_beyond_this_machine(host):
    with pytest.raises(ValidationError):
        WebConfig(host=host, port=5005, max_request_bytes=100, posts_per_minute=10)


def test_finished_run_cannot_be_cancelled(web):
    run_id = start(web)
    for _ in range(2):
        view = wait_for(web, run_id, lambda v: v["question"] or v["report"])
        answer(web, run_id, view, {"action": "approve"})
    wait_for(web, run_id, lambda v: v["report"])
    assert post(web, f"/api/runs/{run_id}/cancel", {}).status_code == 400
    assert get(web, f"/api/runs/{run_id}").get_json()["run"]["status"] == "succeeded"


def test_out_of_date_page_token_gets_a_json_error_with_advice(web):
    response = post(web, "/api/runs", {"repo_url": URL}, token="old")
    assert response.status_code == 403 and "Reload" in response.get_json()["error"]["message"]


def test_events_stream_sends_pings_while_nothing_changes(web, monkeypatch):
    from setuppilot.web import app as web_app

    monkeypatch.setattr(web_app, "_PING_SECONDS", 0)
    monkeypatch.setattr(web_app, "_EVENT_SECONDS", 0)
    run_id = start(web)
    wait_for(web, run_id, lambda v: v["question"])
    response = get(web, f"/api/runs/{run_id}/events")
    chunks = iter(response.response)
    next(chunks)
    assert next(chunks).decode().startswith(": ping")
    response.close()
