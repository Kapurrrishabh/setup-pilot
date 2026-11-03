import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from setuppilot import history
from setuppilot.agent.cli import interact, make_agent, resolve_destination
from setuppilot.agent.driver import Finished, Stopped, Waiting
from setuppilot.chat import cli as chat_cli
from setuppilot.chat.embed import LocalEmbedder
from setuppilot.config import Settings, load_settings
from setuppilot.db.reports import HISTORY_LIMIT
from setuppilot.db.stores import open_stores
from setuppilot.errors import NotFound, SetupPilotError
from setuppilot.evaluation import run_chat, run_setup
from setuppilot.evaluation.chat_eval import load_chat_cases
from setuppilot.evaluation.setup_eval import load_setup_cases
from setuppilot.llm.groq import build_llm_client
from setuppilot.logging_setup import setup_logging
from setuppilot.web.serve import serve

# PROVISIONAL: gpt-oss reasoning tokens count toward this, so leave room past the answer.
ASK_MAX_TOKENS = 1024


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="setuppilot")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="Ask the BIG model one question and show tokens used.")
    ask.add_argument("question")
    commands.add_parser("db-check", help="Write a run to Supabase and read it back.")
    run = commands.add_parser("run", help="Set up a GitHub repo, asking before each command.")
    run.add_argument("repo_url", help="https://github.com/<owner>/<repo>")
    run.add_argument("dest", help="Folder to clone into (it must exist).")
    run.add_argument("--docker", action="store_true", help="Install inside a Docker container.")
    resume = commands.add_parser("resume", help="Continue a paused run.")
    resume.add_argument("run_id")
    commands.add_parser("runs", help="List recent runs with status, time and tokens.")
    show = commands.add_parser("show", help="Show one run's step-by-step timeline.")
    show.add_argument("run_id")
    commands.add_parser("metrics", help="Success rate, average time and tokens, common errors.")
    index = commands.add_parser("index", help="Index a GitHub repo for the chatbot.")
    index.add_argument("repo_url")
    chat = commands.add_parser("chat", help="Ask general questions about an indexed repo.")
    chat.add_argument("repo", help="owner/name, as printed by the index command")
    commands.add_parser("web", help="Open the web app on this machine (http://127.0.0.1:5005).")
    eval_chat = commands.add_parser("eval-chat", help="Score the chatbot on the question set.")
    eval_chat.add_argument("--mode", choices=["full", "retrieval", "replay"], default="full")
    eval_chat.add_argument("--questions", type=Path, default=Path("eval/chat_questions.yaml"))
    eval_chat.add_argument("--results", type=Path, default=Path("eval/results"))
    eval_setup = commands.add_parser("eval-setup", help="Set up every repo in the eval set.")
    eval_setup.add_argument("--repos", type=Path, default=Path("eval/setup_repos.yaml"))
    eval_setup.add_argument("--workspace", type=Path, default=Path("workspace/eval"))
    eval_setup.add_argument("--results", type=Path, default=Path("eval/results"))
    eval_setup.add_argument(
        "--docker", action="store_true", help="Install inside a Docker container."
    )
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    settings = load_settings(Path("config.yaml"))
    setup_logging(settings.logging)
    try:
        if args.command == "ask":
            ask_one_question(settings, args.question)
        elif args.command == "db-check":
            check_database(settings)
        elif args.command in {"runs", "show", "metrics"}:
            show_history(settings, args)
        elif args.command in {"index", "chat"}:
            use_chatbot(settings, args)
        elif args.command == "eval-chat":
            evaluate_chatbot(settings, args)
        elif args.command == "eval-setup":
            stores = open_stores(settings)
            cases = load_setup_cases(args.repos)
            agent = make_agent(settings, stores)
            run_setup.run_setup_eval(
                stores, agent, cases, args.workspace, args.results, args.docker
            )
        elif args.command == "web":
            serve(settings)
        else:
            is_verified = set_up_repo(settings, args)
            raise SystemExit(0 if is_verified else 1)
    except SetupPilotError as error:
        print(f"{error.message}\nDetails: {error.details}", file=sys.stderr)
        raise SystemExit(1) from error
    except NotFound as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error


def ask_one_question(settings: Settings, question: str) -> None:
    stores = open_stores(settings)
    if stores.database_warning:
        print(stores.database_warning, file=sys.stderr)
    client = build_llm_client(settings, stores.usage, stores.cache, stores.metrics)
    result = client.complete(
        [{"role": "user", "content": question}],
        choice="big_then_small",
        max_tokens=ASK_MAX_TOKENS,
        is_json=False,
        run_id=None,
    )
    print(result.text)
    print(
        f"\nmodel: {result.model} | tokens in: {result.input_tokens} | "
        f"tokens out: {result.output_tokens} | from cache: {result.is_cached} | "
        f"cut off: {result.is_truncated}"
    )


def set_up_repo(settings: Settings, args: argparse.Namespace) -> bool:
    stores = open_stores(settings)
    if stores.database_warning:
        print(stores.database_warning, file=sys.stderr)
    agent = make_agent(settings, stores)
    outcome: Waiting | Stopped | Finished
    if args.command == "run":
        outcome = agent.start(args.repo_url, resolve_destination(args.dest), args.docker)
    else:
        if stores.runs.get_run(args.run_id).status == "cancelled":
            print("This run was cancelled. Start a new run instead.", file=sys.stderr)
            return False
        outcome = agent.peek(args.run_id)
    return interact(agent, stores, outcome, input)


def show_history(settings: Settings, args: argparse.Namespace) -> None:
    stores = open_stores(settings)
    if stores.database_warning:
        print(stores.database_warning, file=sys.stderr)
    if args.command == "runs":
        history.print_runs(stores, HISTORY_LIMIT)
    elif args.command == "show":
        history.print_run(stores, args.run_id)
    else:
        history.print_metrics(stores, HISTORY_LIMIT)


def use_chatbot(settings: Settings, args: argparse.Namespace) -> None:
    stores = open_stores(settings)
    if stores.database_warning:
        print(stores.database_warning, file=sys.stderr)
    if args.command == "index":
        chat_cli.index_command(settings, stores, args.repo_url)
    else:
        chat_cli.chat_command(settings, stores, args.repo, input)


def evaluate_chatbot(settings: Settings, args: argparse.Namespace) -> None:
    stores = open_stores(settings)
    if stores.database_warning:
        print(stores.database_warning, file=sys.stderr)
    llm = None
    if args.mode == "full":
        llm = build_llm_client(settings, stores.usage, stores.cache, stores.metrics)
    elif args.mode == "replay":
        llm = run_chat.replay_client(settings, stores)
    embedder = LocalEmbedder(settings.chat.embedding_model, settings.chat.embedding_dimensions)
    cases = load_chat_cases(args.questions)
    run_chat.run_chat_eval(settings, stores, cases, args.mode, args.results, embedder, llm)


def check_database(settings: Settings) -> None:
    stores = open_stores(settings)
    if stores.database_warning:
        print(stores.database_warning, file=sys.stderr)
        raise SystemExit(1)
    run = stores.runs.create_run("https://github.com/setuppilot/db-check", "db-check")
    status = stores.runs.get_run(run.id).status
    stores.runs.delete_run(run.id)
    print(f"Database OK: wrote run {run.id}, read it back ({status}) and deleted it.")


if __name__ == "__main__":
    main()
