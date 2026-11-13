"""The web app through its HTTP surface, with fake repos, a fake runner and a fake LLM."""

import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import get_args

import httpx
import pytest
from keyring.errors import PasswordSetError
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from setuppilot import errors
from setuppilot.agent.driver import SetupAgent
from setuppilot.agent.graph import AgentDeps, build_graph
from setuppilot.chat.bot import RepoChatbot, index_repo
from setuppilot.chat.index import ChatRepo, InMemoryChunkIndex
from setuppilot.config import WebConfig, load_settings
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.runs import InMemoryRunStore, RunStatus
from setuppilot.db.stores import Stores
from setuppilot.github.api import API_URL
from setuppilot.github.device_flow import DEVICE_CODE_URL, REVOKE_URL, TOKEN_URL
from setuppilot.github.vault import KEYRING_SERVICE, KEYRING_USER, binding_hash
from setuppilot.guardrails.commands import VENV_PYTHON
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.usage import InMemoryUsageStore
from setuppilot.logging_setup import run_id_var
from setuppilot.tools.clone import CloneRequest, GitHubAccess
from setuppilot.tools.prereqs import Probe
from setuppilot.web.app import WebServices, create_app
from setuppilot.web.folder_picker import (
    Cancelled,
    FolderPicker,
    FolderWindowFailed,
    NoFolderWindow,
    Picked,
)
from setuppilot.web.github_auth import BINDING_HEADER
from setuppilot.web.labels import ERROR_LABELS, STATUS_LABELS, STEPS, duration_text
from setuppilot.web.preflight import WindowsFacts, run_preflight
from setuppilot.web.runs import CommandTracker, RunManager
from tests.fakes import (
    FAKE_COMMIT,
    FakeClock,
    FakeEmbedder,
    FakeTransport,
    ai_project,
    analysis_reply,
    diagnosis_reply,
    make_client,
    make_repo,
    reply,
)
from tests.github_fakes import (
    APP,
    USER,
    FakeGitHub,
    MemoryKeyring,
    client_for,
    github_access,
    ok,
    repo_json,
    serve_repo,
    targz,
    tree_json,
)
from tests.test_agent import (
    AllInstalled,
    FakeRunner,
    InstalledAfter,
    NothingInstalled,
    fake_clone,
)
from tests.test_config import REAL_CONFIG
from tests.test_github_device_flow import CODE_REPLY, GRANT_REPLY

SETTINGS = load_settings(REAL_CONFIG)
HOST = f"127.0.0.1:{SETTINGS.web.port}"
URL = "https://github.com/someone/demo"
ZENITY = FolderPicker("zenity", "/usr/bin/zenity")


def no_folder_window(picker, start):
    raise AssertionError("this test opens no folder window")


def make_web(
    tmp_path, github: GitHubAccess, *, files=None, agent_llm=None, runner=None, probe=None,
    host="posix", folder_picker=ZENITY, pick_folder=no_folder_window,
):  # fmt: skip
    """The app and its parts: (client, page token, folder, stores, runner, clone requests). The
    agent runs on host with probe, and asks agent_llm (None: no AI) about the repo made of files."""
    files = files or {"requirements.txt": "", "LICENSE": "MIT License"}
    source = make_repo(tmp_path / "source", files)
    (tmp_path / "dest").mkdir()
    stores = Stores(
        InMemoryRunStore(), InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore(),
        InMemoryChunkIndex(), InMemorySaver(), None,
    )  # fmt: skip
    runner = runner or FakeRunner()
    requests: list[CloneRequest] = []

    def clone(ref, dest, request):
        requests.append(request)
        return fake_clone(source)(ref, dest, request)

    settings = SETTINGS.model_copy(
        update={"chat": SETTINGS.chat.model_copy(update={"repos_dir": tmp_path / "chat-repos"})}
    )
    deps = AgentDeps(settings, stores.runs, agent_llm, clone, probe or AllInstalled(), runner, host)
    manager = RunManager(SetupAgent(deps, stores.checkpointer), stores.runs, CommandTracker())
    repo = ChatRepo("someone/demo", URL, FAKE_COMMIT, owner_github_id=None)
    index_repo(repo, source, SETTINGS.chat, 2000, FakeEmbedder(), stores.chat_index)
    answer = json.dumps({"answer": "MIT [1].", "found": True, "sources": [1]})
    llm = make_client(FakeTransport(*[reply(answer)] * 5))
    services = WebServices(
        settings=settings,
        stores=stores,
        runs=manager,
        embedder=FakeEmbedder(),
        make_chatbot=lambda: RepoChatbot(SETTINGS.chat, stores.chat_index, FakeEmbedder(), llm),
        llm_problem=None,
        preflight=lambda folder: [{"name": "Folder", "is_ok": bool(folder), "fix": ""}],
        github=github,
        folder_picker=folder_picker,
        pick_folder=pick_folder,
    )
    app = create_app(services)
    client = app.test_client()
    page = client.get("/", headers={"Host": HOST}).get_data(as_text=True)
    token = page.split('name="csrf-token" content="')[1].split('"')[0]
    return client, token, tmp_path, stores, runner, requests


@pytest.fixture
def web(tmp_path):
    """The app with GitHub sign-in off (no github.app in config.yaml)."""
    return make_web(tmp_path, GitHubAccess(client_for(FakeGitHub()), signed_in=None))


@pytest.fixture
def held_command(monkeypatch):
    """Commands wait for `release`, so their run stays busy; `started` says one is running."""
    started, release, run_command = threading.Event(), threading.Event(), FakeRunner.__call__

    def slow_runner(self, command):
        started.set()
        release.wait(5)
        return run_command(self, command)

    monkeypatch.setattr(FakeRunner, "__call__", slow_runner)
    return started, release


def post(web, path, body, token=None, **headers):
    client, real_token = web[0], web[1]
    headers = {"Host": HOST, "X-CSRF-Token": token if token is not None else real_token} | headers
    return client.post(path, json=body, headers=headers)


def get(web, path, **headers):
    return web[0].get(path, headers={"Host": HOST} | headers)


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
        ({"repo_url": URL}, "the folder"),
        ({"repo_url": "x" * 400, "dest_dir": "/tmp"}, "the GitHub URL"),
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


@pytest.mark.parametrize("action", ["install", "recheck", "done", "stop"])
def test_a_command_question_refuses_answers_meant_for_other_questions(web, action):
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    assert answer(web, run_id, view, {"action": action}).status_code == 200
    after = wait_for(web, run_id, lambda v: v["problem"])
    assert after["problem"]["message"] == "Answer with approve, reject or edit."
    assert after["question_id"] == view["question_id"] and web[4].texts == []


@pytest.fixture
def no_tools(monkeypatch):
    monkeypatch.setattr(AllInstalled, "run", lambda self, command: Probe(False, ""))


def test_the_tool_question_refuses_approve_and_takes_stop(web, no_tools):
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    assert view["question"]["kind"] == "install_tool"
    answer(web, run_id, view, {"action": "approve"})
    refused = wait_for(web, run_id, lambda v: v["problem"])
    assert "recheck or stop" in refused["problem"]["message"]
    assert answer(web, run_id, refused, {"action": "stop"}).status_code == 200
    done = wait_for(web, run_id, lambda v: v["report"])
    assert done["error"]["error_type"] == "PrereqMissing" and web[4].texts == []


def test_every_kind_of_question_pauses_in_a_labelled_step():
    from setuppilot.agent.driver import ANSWERS_BY_KIND
    from setuppilot.web.labels import QUESTION_STEPS

    assert set(QUESTION_STEPS) == set(ANSWERS_BY_KIND) | {"choose_stack"}
    assert set(QUESTION_STEPS.values()) <= set(STEPS)


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


def test_missing_run_opens_a_page_with_a_way_back_not_raw_json(web):
    response = get(web, "/runs/00000000-0000-0000-0000-000000000000")
    page = response.get_data(as_text=True)
    assert response.status_code == 404 and response.mimetype == "text/html"
    assert "Run not found" in page and 'href="/runs"' in page


def test_unknown_address_is_an_html_page_but_unknown_api_stays_json(web):
    assert "Page not found" in get(web, "/no-such-page").get_data(as_text=True)
    assert get(web, "/api/no-such-thing").get_json()["error"]["details"] == "HTTP 404"


def test_run_page_shows_the_repo_name_and_a_utc_time_for_local_display(web):
    page = get(web, f"/runs/{start(web)}").get_data(as_text=True)
    assert ">someone/demo</a>" in page and '<time datetime="' in page


def test_run_page_gives_its_script_the_same_labels_as_the_templates(web):
    page = get(web, f"/runs/{start(web)}").get_data(as_text=True)
    block = page.split('<script type="application/json" id="page-labels">')[1].split("</script>")[0]
    assert json.loads(block)["statuses"] == STATUS_LABELS


def test_labels_cover_every_status_step_and_error():
    graph = build_graph(AgentDeps(SETTINGS, InMemoryRunStore(), None, None, None, None, "posix"))
    error_names = {
        cls.__name__
        for cls in errors.SetupPilotError.__subclasses__()
        if cls.__module__ == errors.__name__
    }
    assert set(STATUS_LABELS) == set(get_args(RunStatus)) | {"paused"}
    assert set(STEPS) == set(graph.nodes)
    assert set(ERROR_LABELS) == error_names


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.4, "under 1 s"), (1.44, "1.4 s"), (12.4, "12 s"), (59.6, "1 min"), (125, "2 min 5 s")]
    + [(3780, "1 h 3 min")],
)
def test_durations_read_like_a_person_would_say_them(seconds, text):
    assert duration_text(seconds) == text


def test_a_blocked_edit_is_a_warning_step_not_a_green_one(web):
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    answer(web, run_id, view, {"action": "edit", "command": "rm -rf /"})
    view = wait_for(web, run_id, lambda v: v["question"] and v["question"]["edit_error"])
    assert [s["tone"] for s in view["steps"] if s["name"] == "approve"] == ["warn"]


def test_a_busy_run_names_the_command_it_is_running_so_a_refresh_still_shows_it(web, held_command):
    _, release = held_command
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    answer(web, run_id, view, {"action": "approve"})
    busy = get(web, f"/api/runs/{run_id}").get_json()
    release.set()
    wait_for(web, run_id, lambda v: v["question"] or v["report"])
    assert busy["running_command"] == view["question"]["command"]


def test_runs_and_metrics_pages_show_plain_words_not_stored_codes(web):
    run_id = start(web)
    wait_for(web, run_id, lambda v: v["question"])
    post(web, f"/api/runs/{run_id}/cancel", {})
    runs, metrics = (get(web, path).get_data(as_text=True) for path in ("/runs", "/metrics"))
    assert ">Cancelled</span>" in runs and ">someone/demo</a>" in runs
    assert "Cancelled by you" in metrics and "RunCancelled" not in metrics


def test_a_run_that_was_running_when_the_app_started_shows_as_paused(web):
    run = web[3].runs.create_run(URL, str(web[2] / "dest"), None)
    web[3].runs.set_status(run.id, "running", None, None)
    # No agent is needed: shown_status never reaches it.
    restarted = RunManager(None, web[3].runs, CommandTracker())
    assert restarted.shown_status(web[3].runs.get_run(run.id)) == "paused"


def test_a_run_another_process_is_working_on_shows_working_and_cannot_be_carried_on(web):
    run = web[3].runs.create_run(URL, str(web[2] / "dest"), None)
    web[3].runs.set_status(run.id, "running", None, None)
    page = get(web, "/runs").get_data(as_text=True)
    assert 'data-status="running">Working</span>' in page and ">Paused</span>" not in page
    response = post(web, f"/api/runs/{run.id}/continue", {})
    assert (
        response.status_code == 400
        and "Another SetupPilot" in response.get_json()["error"]["message"]
    )


def test_runs_page_shows_a_busy_run_as_working_not_paused(web, held_command):
    started, release = held_command
    run_id = start(web)
    answer(web, run_id, wait_for(web, run_id, lambda v: v["question"]), {"action": "approve"})
    started.wait(5)
    page = get(web, "/runs").get_data(as_text=True)
    release.set()
    wait_for(web, run_id, lambda v: v["question"] or v["report"])
    assert 'data-status="running">Working</span>' in page


def test_run_view_names_the_cloned_repo_folder_not_the_folder_the_user_picked(web):
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    assert view["repo_dir"] == str((web[2] / "dest" / "demo").resolve())


def test_run_view_names_no_repo_folder_before_the_repo_was_downloaded(web):
    run = web[3].runs.create_run(URL, str(web[2] / "dest"), None)
    assert get(web, f"/api/runs/{run.id}").get_json()["repo_dir"] is None


def test_events_stream_ends_with_the_finished_view(web):
    run_id = start(web)
    wait_for(web, run_id, lambda v: v["question"])
    post(web, f"/api/runs/{run_id}/cancel", {})
    body = get(web, f"/api/runs/{run_id}/events").get_data(as_text=True)
    last = json.loads(body.strip().split("data: ")[-1])
    assert last["run"]["status"] == "cancelled"


def test_events_stream_stays_open_until_a_cancelled_run_stops_working(
    web, monkeypatch, held_command
):
    from setuppilot.web import app as web_app

    _, release = held_command
    monkeypatch.setattr(web_app, "_EVENT_SECONDS", 0.01)
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    answer(web, run_id, view, {"action": "approve"})
    post(web, f"/api/runs/{run_id}/cancel", {})
    threading.Timer(0.3, release.set).start()
    body = get(web, f"/api/runs/{run_id}/events").get_data(as_text=True)
    last = json.loads(body.strip().split("data: ")[-1])
    assert last["run"]["status"] == "cancelled" and not last["is_busy"]


def test_chat_answers_with_sources(web):
    response = post(
        web, "/api/chat/ask", {"repo": "someone/demo", "question": "Is it MIT License?"}
    )
    assert [c["path"] for c in response.get_json()["citations"]] == ["LICENSE"]


def test_preflight_lists_its_checks(web):
    response = web[0].get(
        "/api/preflight?folder=/tmp", headers={"Host": HOST, "X-CSRF-Token": web[1]}
    )
    assert response.get_json()["checks"][0]["is_ok"]


def test_preflight_needs_the_page_token_because_it_runs_programs(web):
    assert get(web, "/api/preflight?folder=/tmp").status_code == 403


def test_preflight_says_which_failed_checks_stop_a_run(web, monkeypatch):
    from setuppilot.web import preflight

    def database_down(limit):
        raise errors.DatabaseUnavailable("Supabase is not reachable.", details="Check the URL.")

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(preflight, "_github_reads", lambda: {})
    monkeypatch.setattr(web[3].runs, "list_runs", database_down)
    checks = run_preflight(web[3], NothingInstalled(), "/no/such/folder", None)
    failing = {c["id"]: c["is_needed"] for c in checks if not c["is_ok"]}
    assert failing == {"groq": False, "docker": False, "folder": True, "database": True}


class DockerAnswers:
    def __init__(self, os_type):
        self._os_type = os_type

    def run(self, command):
        return Probe(True, self._os_type)


def preflight_by_id(web, monkeypatch, folder, windows, probe=None):
    from setuppilot.web import preflight

    monkeypatch.setattr(preflight, "_github_reads", lambda: {})
    checks = run_preflight(web[3], probe or NothingInstalled(), folder, windows)
    return {c["id"]: c for c in checks}


def test_preflight_on_windows_warns_about_a_folder_inside_onedrive(web, tmp_path, monkeypatch):
    synced = tmp_path / "OneDrive"
    (synced / "work").mkdir(parents=True)
    facts = WindowsFacts((synced,), is_long_paths_enabled=True)
    onedrive = preflight_by_id(web, monkeypatch, str(synced / "work"), facts)["onedrive"]
    assert (onedrive["is_ok"], onedrive["is_needed"]) == (False, False)


def test_preflight_on_windows_says_when_long_paths_are_off(web, monkeypatch):
    facts = WindowsFacts((), is_long_paths_enabled=False)
    assert not preflight_by_id(web, monkeypatch, "", facts)["long_paths"]["is_ok"]


def test_docker_running_windows_containers_is_not_ready_for_docker_mode(web, monkeypatch):
    docker = preflight_by_id(web, monkeypatch, "", None, DockerAnswers("windows"))["docker"]
    assert not docker["is_ok"] and "Linux containers" in docker["fix"]


@contextlib.contextmanager
def unwritable(folder):
    """On Windows os.access says every folder can be written, so this needs a real deny rule."""
    if sys.platform == "win32":
        user = os.environ["USERNAME"]
        subprocess.run(["icacls", str(folder), "/deny", f"{user}:(W)"], check=True)
        try:
            yield
        finally:
            subprocess.run(["icacls", str(folder), "/remove:d", user], check=True)
        return
    folder.chmod(0o500)
    try:
        yield
    finally:
        folder.chmod(0o700)


@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root can write to any folder"
)
def test_preflight_finds_a_folder_it_cannot_write_to(web, tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    with unwritable(locked):
        assert not preflight_by_id(web, monkeypatch, str(locked), None)["folder"]["is_ok"]


def test_cancel_stops_the_command_the_run_is_running(tmp_path):
    stopped = threading.Event()

    class FakeTree:
        was_stopped = False

        def stop(self):
            stopped.set()

    runs, tracker = InMemoryRunStore(), CommandTracker()
    run = runs.create_run(URL, str(tmp_path), None)
    token = run_id_var.set(run.id)
    tracker.track(FakeTree())
    run_id_var.reset(token)
    RunManager(None, runs, tracker).cancel(run.id)
    assert stopped.wait(5)


# "::1" is loopback, but the app binds and prints 127.0.0.1, so it would print a dead address.
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "::1"])
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


# GitHub sign-in, and what only the browser that signed in may see

TOKEN = "ghu_webSecret0123456789"  # noqa: S105 - a fake token
# github_access signs octocat in from the browser that holds this key.
BOUND = {BINDING_HEADER: "browser-key"}
PRIVATE_URL = "https://github.com/someone/secret-plans"
INSTALLATIONS_URL = f"{API_URL}/user/installations?per_page=100"
INSTALLATION = {"id": 7, "account": {"login": "octocat"}, "repository_selection": "selected"}


@dataclass
class GitHubWeb:
    web: tuple
    fake: FakeGitHub
    access: GitHubAccess
    keyring: MemoryKeyring
    clock: FakeClock


def github_web(tmp_path, token):
    fake, clock, keyring = FakeGitHub(), FakeClock(), MemoryKeyring()
    access = github_access(fake, clock, keyring, token)
    return GitHubWeb(make_web(tmp_path, access), fake, access, keyring, clock)


@pytest.fixture
def signed(tmp_path):
    """Sign-in on, and octocat signed in from the browser that sends BOUND."""
    return github_web(tmp_path, TOKEN)


@pytest.fixture
def signed_out(tmp_path):
    """Sign-in on, nobody signed in yet, and GitHub ready to say yes to a sign-in."""
    gh = github_web(tmp_path, None)
    gh.fake.on("POST", DEVICE_CODE_URL, ok(CODE_REPLY))
    gh.fake.on("POST", TOKEN_URL, ok(GRANT_REPLY))
    gh.fake.on("GET", f"{API_URL}/user", ok({"login": "octocat", "id": USER.id}))
    gh.fake.on("POST", REVOKE_URL, httpx.Response(202))
    return gh


def sign_in(gh) -> str:
    """Go through the device flow; returns the key the browser gets."""
    started = post(gh.web, "/api/github/signin", {}).get_json()
    gh.clock.sleep(CODE_REPLY["interval"])
    done = post(gh.web, "/api/github/signin/poll", {"pending_id": started["pending_id"]})
    return done.get_json()["binding_key"]


def requests_to(gh, url):
    return [r for r in gh.fake.requests if str(r.url) == url]


def sign_in_as(gh, login, user_id) -> dict[str, str]:
    """Sign this GitHub user in; returns the headers their browser sends."""
    gh.fake.on("GET", f"{API_URL}/user", ok({"login": login, "id": user_id}))
    return {BINDING_HEADER: sign_in(gh)}


def start_signed_in_run(gh, headers=BOUND):
    body = {"repo_url": PRIVATE_URL, "dest_dir": str(gh.web[2] / "dest"), "access": "github"}
    response = post(gh.web, "/api/runs", body, **headers)
    assert response.status_code == 200, response.get_json()
    run_id = response.get_json()["run_id"]
    for _ in range(200):
        view = get(gh.web, f"/api/runs/{run_id}", **headers).get_json()
        if not view["is_busy"] and view["question"]:
            return run_id, view
        time.sleep(0.02)
    raise AssertionError(f"the run never asked a question: {view}")


def test_without_a_github_app_every_github_endpoint_says_how_to_turn_sign_in_on(web):
    status = get(web, "/api/github/status").get_json()
    assert not status["is_enabled"] and "github.app" in status["turn_on"]
    dest = str(web[2] / "dest")
    responses = [
        post(web, "/api/github/signin", {}),
        post(web, "/api/github/signin/poll", {"pending_id": "x"}),
        post(web, "/api/github/signout", {}),
        get(web, "/api/github/repos"),
        get(web, "/api/github/runs"),
        post(web, "/api/runs", {"repo_url": URL, "dest_dir": dest, "access": "github"}),
        post(web, "/api/chat/index", {"repo_url": URL, "access": "github"}),
    ]
    for response in responses:
        assert response.status_code == 401
        assert "github.app" in response.get_json()["error"]["details"]


def test_sign_in_hands_the_browser_its_key_once_and_keeps_only_its_hash(signed_out):
    started = post(signed_out.web, "/api/github/signin", {}).get_json()
    signed_out.clock.sleep(CODE_REPLY["interval"])
    poll = {"pending_id": started["pending_id"]}
    done = post(signed_out.web, "/api/github/signin/poll", poll).get_json()
    again = post(signed_out.web, "/api/github/signin/poll", poll).get_json()
    saved = signed_out.keyring.items[(KEYRING_SERVICE, KEYRING_USER)]
    assert (done["state"], done["login"]) == ("done", "octocat")
    assert done["binding_key"] not in saved and binding_hash(done["binding_key"]) in saved
    assert again["state"] == "failed" and "binding_key" not in again


def test_the_device_code_never_reaches_the_browser(signed_out):
    response = post(signed_out.web, "/api/github/signin", {})
    started = response.get_json()
    assert CODE_REPLY["device_code"] not in response.get_data(as_text=True)
    assert started["user_code"] == CODE_REPLY["user_code"] and "github.com" in started["warning"]


def test_polling_often_asks_github_only_as_often_as_it_allows(signed_out):
    started = post(signed_out.web, "/api/github/signin", {}).get_json()
    poll = {"pending_id": started["pending_id"]}
    states = [post(signed_out.web, "/api/github/signin/poll", poll).get_json() for _ in range(3)]
    assert [s["state"] for s in states] == ["pending"] * 3
    assert requests_to(signed_out, TOKEN_URL) == []
    signed_out.clock.sleep(CODE_REPLY["interval"])
    post(signed_out.web, "/api/github/signin/poll", poll)
    assert len(requests_to(signed_out, TOKEN_URL)) == 1


def test_a_poll_for_a_sign_in_that_was_replaced_or_never_started_gets_nothing(signed_out):
    first = post(signed_out.web, "/api/github/signin", {}).get_json()
    post(signed_out.web, "/api/github/signin", {})
    signed_out.clock.sleep(CODE_REPLY["interval"])
    for pending_id in (first["pending_id"], "a-guess", None):
        polled = post(signed_out.web, "/api/github/signin/poll", {"pending_id": pending_id})
        assert polled.get_json()["state"] == "failed"
    assert requests_to(signed_out, TOKEN_URL) == []


def test_a_new_sign_in_is_refused_while_someone_is_signed_in(signed):
    assert post(signed.web, "/api/github/signin", {}).status_code == 409
    assert signed.fake.requests == []


def test_status_names_the_user_only_to_the_browser_that_signed_in(signed):
    mine = get(signed.web, "/api/github/status", **BOUND).get_json()
    other = get(signed.web, "/api/github/status").get_json()
    assert (mine["is_this_browser"], mine["login"]) == (True, "octocat")
    assert (other["is_signed_in"], other["is_this_browser"], other["login"]) == (True, False, None)


@pytest.mark.parametrize(
    "headers", [{}, {BINDING_HEADER: "wrong"}, {BINDING_HEADER: ""}], ids=["none", "wrong", "empty"]
)
def test_the_repo_list_is_refused_without_this_browsers_key(signed, headers):
    response = get(signed.web, "/api/github/repos", **headers)
    assert response.status_code == 401 and signed.fake.requests == []


def test_the_repo_list_follows_pages_and_shows_what_picking_a_repo_needs(signed):
    second = f"{API_URL}/user/installations/7/repositories?per_page=100&page=2"
    signed.fake.on(
        "GET", INSTALLATIONS_URL, ok({"total_count": 1, "installations": [INSTALLATION]})
    )
    signed.fake.on(
        "GET", f"{API_URL}/user/installations/7/repositories?per_page=100",
        ok({"repositories": [repo_json(is_private=True)]}, link=f'<{second}>; rel="next"'),
    )  # fmt: skip
    signed.fake.on("GET", second, ok({"repositories": [repo_json() | {"full_name": "o/lib"}]}))
    listed = get(signed.web, "/api/github/repos", **BOUND).get_json()
    first = listed["repos"][0]
    assert [r["full_name"] for r in listed["repos"]] == ["o/r", "o/lib"]
    assert (first["url"], first["visibility"], first["language"], first["default_branch"]) == (
        "https://github.com/o/r", "private", "Python", "main",
    )  # fmt: skip
    assert first["description"] == "A demo" and listed["is_complete"]
    assert signed.fake.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_with_no_installations_the_list_is_empty_and_gives_the_install_link(signed):
    signed.fake.on("GET", INSTALLATIONS_URL, ok({"total_count": 0, "installations": []}))
    listed = get(signed.web, "/api/github/repos", **BOUND).get_json()
    assert (listed["repos"], listed["install_url"]) == ([], APP.install_url)


def test_sign_out_forgets_the_sign_in_and_cancels_it_at_github(signed):
    signed.fake.on("POST", REVOKE_URL, httpx.Response(202))
    signed_out = post(signed.web, "/api/github/signout", {}).get_json()
    (revoke,) = requests_to(signed, REVOKE_URL)
    assert signed_out["warning"] is None and "Authorization" not in revoke.headers
    assert json.loads(revoke.content)["credentials"] == [TOKEN, "ghr_testtoken"]
    assert signed.keyring.items == {}
    assert get(signed.web, "/api/github/repos", **BOUND).status_code == 401


def test_sign_out_that_github_does_not_confirm_says_how_to_finish_it(signed):
    signed.fake.on("POST", REVOKE_URL, httpx.Response(500))
    warning = post(signed.web, "/api/github/signout", {}).get_json()["warning"]
    assert "Authorized GitHub Apps" in warning["details"] and signed.keyring.items == {}


def test_the_key_from_an_earlier_sign_in_stops_working(signed_out):
    old_key = sign_in(signed_out)
    post(signed_out.web, "/api/github/signout", {})
    new_key = sign_in(signed_out)
    signed_out.fake.on("GET", INSTALLATIONS_URL, ok({"total_count": 0, "installations": []}))
    assert get(signed_out.web, "/api/github/repos", **{BINDING_HEADER: old_key}).status_code == 401
    assert get(signed_out.web, "/api/github/repos", **{BINDING_HEADER: new_key}).status_code == 200


def test_a_sign_in_that_cannot_be_saved_is_cancelled_at_github(signed_out):
    signed_out.keyring.set_error = PasswordSetError("The keyring is locked.")
    started = post(signed_out.web, "/api/github/signin", {}).get_json()
    signed_out.clock.sleep(CODE_REPLY["interval"])
    poll = post(signed_out.web, "/api/github/signin/poll", {"pending_id": started["pending_id"]})
    (revoke,) = requests_to(signed_out, REVOKE_URL)
    assert poll.status_code == 400 and "Could not save" in poll.get_json()["error"]["message"]
    assert json.loads(revoke.content)["credentials"] == ["ghu_new", "ghr_new"]


RUN_CALLS = {
    "view": lambda gh, run_id, view, h: get(gh.web, f"/api/runs/{run_id}", **h),
    "events": lambda gh, run_id, view, h: get(gh.web, f"/api/runs/{run_id}/events", **h),
    "answer": lambda gh, run_id, view, h: post(
        gh.web,
        f"/api/runs/{run_id}/answer",
        {"action": "reject", "question_id": view["question_id"]},
        **h,
    ),
    "cancel": lambda gh, run_id, view, h: post(gh.web, f"/api/runs/{run_id}/cancel", {}, **h),
    "continue": lambda gh, run_id, view, h: post(gh.web, f"/api/runs/{run_id}/continue", {}, **h),
}


@pytest.mark.parametrize("call", RUN_CALLS)
def test_a_signed_in_run_is_refused_to_a_browser_without_the_key(signed, call):
    run_id, view = start_signed_in_run(signed)
    for headers in ({}, {BINDING_HEADER: "wrong"}):
        response = RUN_CALLS[call](signed, run_id, view, headers)
        assert response.status_code == 401, response.get_data(as_text=True)
    assert (
        get(signed.web, f"/api/runs/{run_id}", **BOUND).get_json()["question"] == view["question"]
    )


@pytest.mark.parametrize("call", RUN_CALLS)
def test_a_signed_in_run_answers_the_browser_that_signed_in(signed, call):
    run_id, view = start_signed_in_run(signed)
    response = RUN_CALLS[call](signed, run_id, view, BOUND)
    response.close()
    # continue says there is nothing to carry on (400): the point is it got past the key check.
    assert response.status_code in (200, 400) and response.status_code != 401


@pytest.mark.parametrize("call", RUN_CALLS)
def test_public_runs_need_no_key_while_someone_is_signed_in(signed, call):
    run_id = start(signed.web)
    view = wait_for(signed.web, run_id, lambda v: v["question"])
    response = RUN_CALLS[call](signed, run_id, view, {})
    response.close()
    assert response.status_code in (200, 400)


def test_starting_a_signed_in_run_needs_this_browsers_key_not_just_the_page_token(signed):
    body = {"repo_url": PRIVATE_URL, "dest_dir": str(signed.web[2] / "dest"), "access": "github"}
    assert post(signed.web, "/api/runs", body).status_code == 401
    assert signed.web[3].runs.list_runs(10) == []


def test_a_signed_in_runs_access_and_sub_folder_reach_the_clone_and_its_user_is_saved(signed):
    body = {"repo_url": PRIVATE_URL, "dest_dir": str(signed.web[2] / "dest"), "access": "github",
            "subdir": "packages/web"}  # fmt: skip
    run_id = post(signed.web, "/api/runs", body, **BOUND).get_json()["run_id"]
    for _ in range(200):
        if signed.web[5]:
            break
        time.sleep(0.02)
    assert signed.web[5] == [CloneRequest("github", "packages/web")]
    assert signed.web[3].runs.get_run(run_id).owner_github_id == USER.id


@pytest.mark.parametrize(
    ("extra", "message"),
    [({"access": "ssh"}, "public repo, or one from your GitHub"),
     ({"subdir": "../up"}, "sub-folder must be a folder"), ({"subdir": 5}, "sub-folder")],
)  # fmt: skip
def test_a_strange_access_or_sub_folder_is_refused(web, extra, message):
    body = {"repo_url": URL, "dest_dir": str(web[2] / "dest")} | extra
    response = post(web, "/api/runs", body)
    assert response.status_code == 400 and message in response.get_json()["error"]["message"]


def test_runs_page_and_run_page_leave_out_a_signed_in_runs_repo_and_folder(signed):
    run_id, _ = start_signed_in_run(signed)
    pages = get(signed.web, "/runs").get_data(as_text=True) + get(
        signed.web, f"/runs/{run_id}"
    ).get_data(as_text=True)
    assert "secret-plans" not in pages and str(signed.web[2] / "dest") not in pages
    assert get(signed.web, "/api/github/runs").status_code == 401
    listed = get(signed.web, "/api/github/runs", **BOUND).get_json()["runs"]
    assert [(r["id"], r["repo_url"]) for r in listed] == [(run_id, PRIVATE_URL)]


def octocats_run_then_mallory_signs_in(gh):
    """octocat starts a signed-in run and signs out, then mallory signs in. Returns the run id,
    its view and mallory's headers."""
    run_id, view = start_signed_in_run(gh, sign_in_as(gh, "octocat", USER.id))
    post(gh.web, "/api/github/signout", {})
    return run_id, view, sign_in_as(gh, "mallory", 999)


@pytest.mark.parametrize("call", RUN_CALLS)
def test_a_signed_in_run_is_not_found_by_another_github_user(signed_out, call):
    run_id, view, mallory = octocats_run_then_mallory_signs_in(signed_out)
    response = RUN_CALLS[call](signed_out, run_id, view, mallory)
    assert response.status_code == 404 and "secret-plans" not in response.get_data(as_text=True)


def test_another_github_users_run_is_not_in_the_signed_in_runs_list(signed_out):
    _, _, mallory = octocats_run_then_mallory_signs_in(signed_out)
    assert get(signed_out.web, "/api/github/runs", **mallory).get_json()["runs"] == []


def test_the_same_github_user_signing_in_again_gets_their_run_back(signed_out):
    run_id, _ = start_signed_in_run(signed_out, sign_in_as(signed_out, "octocat", USER.id))
    post(signed_out.web, "/api/github/signout", {})
    again = sign_in_as(signed_out, "octocat", USER.id)
    listed = get(signed_out.web, "/api/github/runs", **again).get_json()["runs"]
    view = get(signed_out.web, f"/api/runs/{run_id}", **again).get_json()
    assert [r["id"] for r in listed] == [run_id] and view["run"]["repo_url"] == PRIVATE_URL


def test_the_live_view_of_a_signed_in_run_stops_after_sign_out(signed, monkeypatch):
    from setuppilot.web import app as web_app

    monkeypatch.setattr(web_app, "_PING_SECONDS", 0)
    monkeypatch.setattr(web_app, "_EVENT_SECONDS", 0)
    run_id, _ = start_signed_in_run(signed)
    response = get(signed.web, f"/api/runs/{run_id}/events", **BOUND)
    signed.access.signed_in.vault.clear()
    # The test client reads the first event at once; a stream that went on would ping next.
    sent = list(itertools.islice(response.response, 5))
    assert len(sent) == 1 and sent[0].startswith(b"data: ")


def index_private_repo(gh, owner):
    source = gh.web[2] / "source"
    repo = ChatRepo("someone/secret-plans", PRIVATE_URL, FAKE_COMMIT, owner_github_id=owner)
    index_repo(repo, source, SETTINGS.chat, 2000, FakeEmbedder(), gh.web[3].chat_index)


def test_a_private_chat_repo_is_left_out_of_the_chat_page_and_listed_only_for_its_owner(signed):
    index_private_repo(signed, USER.id)
    assert "secret-plans" not in get(signed.web, "/chat").get_data(as_text=True)
    assert "someone/secret-plans" not in get(signed.web, "/api/chat/repos").get_json()["repos"]
    mine = get(signed.web, "/api/chat/repos", **BOUND).get_json()["repos"]
    assert "someone/secret-plans" in mine


def test_a_private_chat_repo_answers_only_the_browser_that_signed_in(signed):
    index_private_repo(signed, USER.id)
    question = {"repo": "someone/secret-plans", "question": "Is it MIT License?"}
    assert post(signed.web, "/api/chat/ask", question).status_code == 404
    answered = post(signed.web, "/api/chat/ask", question, **BOUND).get_json()
    assert answered["status"] == "answered"


def test_indexing_through_the_sign_in_needs_this_browsers_key(signed):
    body = {"repo_url": "https://github.com/o/r", "access": "github"}
    assert post(signed.web, "/api/chat/index", body).status_code == 401
    assert signed.fake.requests == []


def test_a_private_repo_indexed_through_the_sign_in_is_kept_for_that_user_only(signed):
    files = {"README.md": b"# Secret plans\n", "LICENSE": b"MIT License\n"}
    serve_repo(signed.fake, targz(files), tree_json(files), is_private=True)
    body = {"repo_url": "https://github.com/o/r", "access": "github"}
    indexed = post(signed.web, "/api/chat/index", body, **BOUND).get_json()
    index = signed.web[3].chat_index
    assert indexed["repo"] == "o/r" and indexed["chunks"] > 0
    assert ("o/r" in index.repos(None), "o/r" in index.repos(USER.id)) == (False, True)
    assert signed.fake.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


# The Browse button's folder window, opened by the server


def picking(tmp_path, pick, picker=ZENITY):
    github = GitHubAccess(client_for(FakeGitHub()), signed_in=None)
    return make_web(tmp_path, github, folder_picker=picker, pick_folder=pick)


def test_pick_folder_needs_the_page_token_and_a_json_body(tmp_path):
    opened = []
    web = picking(tmp_path, lambda picker, start: opened.append(start) or Cancelled(None))
    assert post(web, "/api/pick-folder", {}, token="wrong").status_code == 403
    headers = {"Host": HOST, "X-CSRF-Token": web[1]}
    form = web[0].post("/api/pick-folder", data={"start": "/"}, headers=headers)
    assert form.status_code == 400 and opened == []
    assert post(web, "/api/pick-folder", {}).get_json() == {"status": "cancelled", "reason": None}


def test_a_picked_folder_comes_back_as_a_full_checked_path(tmp_path):
    web = picking(tmp_path, lambda picker, start: Picked(tmp_path / "dest" / ".."))
    picked = post(web, "/api/pick-folder", {}).get_json()
    assert picked == {"status": "picked", "path": str(tmp_path.resolve()), "picker": "zenity"}


@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root can write to any folder"
)
def test_pick_folder_refuses_a_folder_it_cannot_write(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    web = picking(tmp_path, lambda picker, start: Picked(locked))
    with unwritable(locked):
        response = post(web, "/api/pick-folder", {})
    assert response.status_code == 400
    assert "cannot use that folder" in response.get_json()["error"]["message"]


def test_the_window_opens_at_the_folder_typed_in_or_else_the_home_folder(tmp_path):
    starts = []
    web = picking(tmp_path, lambda picker, start: starts.append(start) or Cancelled(None))
    for typed in (str(tmp_path / "dest"), str(tmp_path / "not-made-yet"), ""):
        post(web, "/api/pick-folder", {"start": typed})
    home = Path.home().resolve()
    assert starts == [(tmp_path / "dest").resolve(), home, home]
    assert post(web, "/api/pick-folder", {"start": 5}).status_code == 400


def test_a_second_folder_window_while_one_is_open_is_refused_as_busy(tmp_path):
    opened, close = threading.Event(), threading.Event()

    def slow(picker, start):
        opened.set()
        close.wait(5)
        return Cancelled(None)

    web = picking(tmp_path, slow)
    first = threading.Thread(target=post, args=(web, "/api/pick-folder", {}))
    first.start()
    assert opened.wait(5)
    second = post(web, "/api/pick-folder", {})
    close.set()
    first.join(5)
    assert second.status_code == 409 and "already open" in second.get_json()["error"]["message"]
    assert post(web, "/api/pick-folder", {}).status_code == 200


def test_a_window_closed_after_too_long_says_so_and_picks_nothing(tmp_path):
    reason = "The folder window was open for 10 minutes, so SetupPilot closed it."
    web = picking(tmp_path, lambda picker, start: Cancelled(reason))
    assert post(web, "/api/pick-folder", {}).get_json() == {"status": "cancelled", "reason": reason}


def test_a_window_that_fails_to_open_is_a_clear_error_and_frees_the_button(tmp_path):
    def broken(picker, start):
        raise FolderWindowFailed("Could not open a folder window.", details="zenity: exit 255.")

    web = picking(tmp_path, broken)
    responses = [post(web, "/api/pick-folder", {}) for _ in range(2)]
    assert [r.status_code for r in responses] == [400, 400]
    assert responses[1].get_json()["error"]["message"] == "Could not open a folder window."


def test_without_a_folder_window_there_is_no_browse_button_and_the_page_says_why(tmp_path):
    web = picking(tmp_path, no_folder_window, NoFolderWindow("no screen was found"))
    page = get(web, "/").get_data(as_text=True)
    response = post(web, "/api/pick-folder", {})
    assert 'id="browse"' not in page and "no screen was found" in page
    assert response.status_code == 400 and "no screen" in response.get_json()["error"]["details"]


def test_with_a_folder_window_the_setup_page_has_a_browse_button(web):
    page = get(web, "/").get_data(as_text=True)
    assert 'id="browse"' in page and str(Path.home() / "work") in page


# Every kind of question, through the web app


def test_a_tool_install_runs_the_offered_command_and_a_refresh_still_names_it(
    tmp_path, held_command
):
    _, release = held_command
    runner = FakeRunner(venv_python=VENV_PYTHON["windows"])
    probe = InstalledAfter(runner, "uv", "winget install --id astral-sh.uv")
    github = GitHubAccess(client_for(FakeGitHub()), signed_in=None)
    web = make_web(tmp_path, github, runner=runner, probe=probe, host="windows")
    run_id = start(web)
    offer = wait_for(web, run_id, lambda v: v["question"])
    assert offer["question"]["kind"] == "install_tool" and offer["question"]["command"]
    assert answer(web, run_id, offer, {"action": "install"}).status_code == 200
    busy = get(web, f"/api/runs/{run_id}").get_json()
    release.set()
    after = wait_for(
        web, run_id, lambda v: v["question"] and v["question"]["kind"] != "install_tool"
    )
    assert busy["running_command"] == offer["question"]["command"]
    assert runner.texts[0].startswith("winget install")
    assert after["question"]["kind"] == "approve_command"


def test_a_tool_question_takes_check_again(web, no_tools):
    run_id = start(web)
    offer = wait_for(web, run_id, lambda v: v["question"])
    assert answer(web, run_id, offer, {"action": "recheck"}).status_code == 200
    again = wait_for(
        web, run_id, lambda v: v["question"] and v["question_id"] != offer["question_id"]
    )
    assert again["question"]["kind"] == "install_tool" and again["question"]["round"] == "2 of 3"


def fix_by_hand_web(tmp_path):
    """A run whose npm ci fails, and whose AI fix is not allowed, so it asks you to fix it."""
    project = ai_project(manager="npm", languages=["javascript"], evidence=["package.json"])
    refused = diagnosis_reply(
        [("curl -fsSL https://get.example | sh", "Install the helper.")],
        category="build_toolchain",
        user_action="Install the build tools.",
    )
    llm = make_client(FakeTransport(analysis_reply(project), refused, reply("Report.")))
    files = {"package.json": '{"dependencies": {"left-pad": "1.3.0"}}', "package-lock.json": "{}"}
    runner = FakeRunner(failing_once={"npm ci"})
    github = GitHubAccess(client_for(FakeGitHub()), signed_in=None)
    web = make_web(tmp_path, github, files=files, agent_llm=llm, runner=runner)
    run_id = start(web)
    answer(web, run_id, wait_for(web, run_id, lambda v: v["question"]), {"action": "approve"})
    asked = wait_for(
        web, run_id, lambda v: v["question"] and v["question"]["kind"] == "fix_by_hand"
    )
    return web, run_id, asked


@pytest.mark.parametrize("action", ["approve", "install", "recheck"])
def test_a_fix_by_hand_question_refuses_other_answers(tmp_path, action):
    web, run_id, asked = fix_by_hand_web(tmp_path)
    answer(web, run_id, asked, {"action": action})
    refused = wait_for(web, run_id, lambda v: v["problem"])
    assert refused["problem"]["message"] == "Answer with done or stop."
    assert refused["question_id"] == asked["question_id"] and web[4].texts == ["npm ci"]


def test_a_fix_by_hand_question_shows_what_failed_and_done_runs_the_step_again(tmp_path):
    web, run_id, asked = fix_by_hand_web(tmp_path)
    question = asked["question"]
    assert (question["command"], question["user_action"]) == ("npm ci", "Install the build tools.")
    assert question["diagnosis"]["category"] == "build_toolchain"
    (diagnose,) = [s for s in asked["steps"] if s["name"] == "diagnose"]
    assert diagnose["arguments"]["refused"][0]["command"].startswith("curl")
    answer(web, run_id, asked, {"action": "done"})
    again = wait_for(
        web, run_id, lambda v: v["question"] and v["question_id"] != asked["question_id"]
    )
    answer(web, run_id, again, {"action": "approve"})
    wait_for(web, run_id, lambda v: v["question"] or v["report"])
    assert web[4].texts[:2] == ["npm ci", "npm ci"]


def test_a_fix_by_hand_question_takes_stop_and_the_run_ends(tmp_path):
    web, run_id, asked = fix_by_hand_web(tmp_path)
    answer(web, run_id, asked, {"action": "stop"})
    done = wait_for(web, run_id, lambda v: v["report"])
    assert done["error"]["error_type"] == "InstallFailed" and web[4].texts == ["npm ci"]


def test_the_run_view_carries_the_analysis_the_run_page_draws(tmp_path):
    analysis = analysis_reply(ai_project(), summary="A demo app.")
    llm = make_client(FakeTransport(analysis, reply("Report.")))
    github = GitHubAccess(client_for(FakeGitHub()), signed_in=None)
    web = make_web(tmp_path, github, agent_llm=llm)
    run_id = start(web)
    view = wait_for(web, run_id, lambda v: v["question"])
    (analyze,) = [s for s in view["steps"] if s["name"] == "analyze"]
    assert analyze["arguments"]["summary"] == "A demo app."
    option = {"id": "python/pip", "source": "both", "is_runnable": True, "note": None}
    assert option in analyze["arguments"]["options"]


def test_every_way_to_set_up_and_every_visibility_has_a_label():
    from setuppilot.analysis.reconcile import Source
    from setuppilot.github.api import RepoInfo
    from setuppilot.web.labels import SOURCE_LABELS, VISIBILITY_LABELS

    assert set(SOURCE_LABELS) == set(get_args(Source))
    assert set(VISIBILITY_LABELS) == set(get_args(RepoInfo.__annotations__["visibility"]))


@pytest.mark.parametrize("path", ["/", "/runs", "/chat"])
def test_pages_with_a_script_carry_the_labels_and_no_others_do(web, path):
    page = get(web, path).get_data(as_text=True)
    block = page.split('<script type="application/json" id="page-labels">')[1].split("</script>")[0]
    assert json.loads(block)["sources"]["both"]
    assert 'id="page-labels"' not in get(web, "/metrics").get_data(as_text=True)


# GitHub on the pages: never the login or the key in HTML, and what each browser may see


def test_the_signed_in_run_page_shows_a_placeholder_until_the_script_reads_the_run(signed):
    run_id, _ = start_signed_in_run(signed)
    page = get(signed.web, f"/runs/{run_id}").get_data(as_text=True)
    assert ">Private repo</a>" in page and "Read through your GitHub sign-in" in page


def test_no_page_holds_the_login_or_the_browser_key(signed):
    paths = ("/", "/runs", "/chat", "/settings")
    pages = "".join(get(signed.web, path, **BOUND).get_data(as_text=True) for path in paths)
    assert "octocat" not in pages and "browser-key" not in pages and TOKEN not in pages


def test_settings_says_how_to_turn_sign_in_on_when_it_is_off(web):
    page = get(web, "/settings").get_data(as_text=True)
    assert "Sign in with GitHub is off." in page and "#sign-in-with-github" in page


def test_settings_links_to_the_repo_choice_and_says_when_sign_in_is_memory_only(tmp_path):
    access = github_access(FakeGitHub(), FakeClock(), None, None)
    page = get(make_web(tmp_path, access), "/settings").get_data(as_text=True)
    assert APP.install_url in page and "kept in memory only" in page


def test_the_signed_in_runs_list_says_how_long_each_took(signed):
    run_id, _ = start_signed_in_run(signed)
    post(signed.web, f"/api/runs/{run_id}/cancel", {}, **BOUND)
    (listed,) = get(signed.web, "/api/github/runs", **BOUND).get_json()["runs"]
    assert listed["took"] == duration_text(listed["seconds"])


def test_the_chat_repo_list_marks_private_repos_only_for_their_owner(signed):
    index_private_repo(signed, USER.id)
    mine = get(signed.web, "/api/chat/repos", **BOUND).get_json()
    other = get(signed.web, "/api/chat/repos").get_json()
    assert mine["private"] == ["someone/secret-plans"] and "someone/demo" in mine["repos"]
    assert other == {"repos": ["someone/demo"], "private": []}


# Pre-flight


class RateLimit:
    """Stands in for urlopen: records the URL and answers with GitHub's read limit."""

    def __init__(self, error=None):
        self.urls, self._error = [], error

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        body = {"resources": {"core": {"limit": 60, "remaining": 57, "used": 3, "reset": 0}}}
        return contextlib.nullcontext(io.BytesIO(json.dumps(body).encode()))


def internet_row(web, monkeypatch, urlopen):
    from setuppilot.web import preflight

    monkeypatch.setattr(preflight.urllib.request, "urlopen", urlopen)
    checks = run_preflight(web[3], NothingInstalled(), "", None)
    return next(c for c in checks if c["id"] == "internet")


def test_preflight_asks_github_for_its_free_read_limit_and_shows_it(web, monkeypatch):
    urlopen = RateLimit()
    row = internet_row(web, monkeypatch, urlopen)
    assert urlopen.urls == ["https://api.github.com/rate_limit"]
    assert row["is_ok"] and row["note"] == "57 of 60 public GitHub reads left this hour."


def test_preflight_takes_any_answer_from_github_as_internet_and_no_answer_as_none(web, monkeypatch):
    refused = urllib.error.HTTPError(
        "https://api.github.com/rate_limit", 403, "Forbidden", Message(), None
    )
    answered = internet_row(web, monkeypatch, RateLimit(refused))
    offline = internet_row(web, monkeypatch, RateLimit(urllib.error.URLError("no route")))
    assert (answered["is_ok"], answered["note"]) == (True, "")
    assert (offline["is_ok"], offline["is_needed"]) == (False, True)


def test_preflight_says_what_the_ai_does_without_a_key(web, monkeypatch):
    from setuppilot.web import preflight

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(preflight, "_github_reads", lambda: {})
    checks = run_preflight(web[3], NothingInstalled(), "", None)
    groq = next(c for c in checks if c["id"] == "groq")
    assert "the AI analysis, fixes and report" in groq["fix"] and "README" not in groq["fix"]
