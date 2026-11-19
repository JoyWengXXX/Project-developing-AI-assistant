"""問答模型對照評估的命令列介面與資料庫後端（第四階段 4-5）；腳本是 `scripts/eval/run_qa_eval.py`。

薄的一層：解析參數、讀資料集、備妥供應商與價格、接上資料庫（檢索與同意檢查），其餘邏輯都在
`repopilot.qa.evaluation`。檢索與問答 worker 共用 `qa_question_runner.retrieve_context`，
所以所有候選模型看到的輸入與正式問答流程相同。

.NET 對照：像一支 console 專案，`Main` 只做組態與相依注入，商業邏輯在函式庫。
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Repository, RepositoryQaConsent, Snapshot
from repopilot.db.session import create_db_engine, create_session_factory
from repopilot.embedding.gateway_client import embed_query
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import RepoPilotError
from repopilot.qa import evaluation
from repopilot.qa.context import ContextResult
from repopilot.qa.evaluation import (
    ConsentRefusedError,
    Dataset,
    EvalBackend,
    EvalConfigError,
    JsonlSink,
    ProviderFactory,
    build_header,
    build_providers,
    collect_environment,
    default_provider_factory,
    load_dataset,
    parse_candidates,
    resolve_prices,
    run_evaluation,
)
from repopilot.qa.question_rules import DEFAULT_MAX_TOKENS, MAX_TOKENS_CEILING
from repopilot.services import qa_question_runner

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_COST_CAP = 3


class DbEvalBackend:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        snapshot_ids: Sequence[int],
        embed: Callable[[str], list[float]],
    ) -> None:
        self._session = session
        self._settings = settings
        self._snapshot_ids = list(snapshot_ids)
        self._embed = embed

    def unconsented_repositories(self) -> list[str]:
        rows = self._session.execute(
            select(Repository.id, Repository.name)
            .join(Snapshot, Snapshot.repository_id == Repository.id)
            .where(Snapshot.id.in_(self._snapshot_ids))
            .distinct()
        ).all()
        consented = set(self._session.scalars(select(RepositoryQaConsent.repository_id)))
        self._session.rollback()  # 不讓長時間的評估持有開著的交易
        return sorted(name for repository_id, name in rows if repository_id not in consented)

    def retrieve(self, question: str) -> ContextResult:
        retrieval = qa_question_runner.retrieve_context(
            self._session, self._settings, self._snapshot_ids, question, embed=self._embed
        )
        self._session.commit()  # 結束檢索的讀取交易（SET LOCAL statement_timeout）
        return retrieval.context


def resolve_snapshot_ids(session: Session, dataset: Dataset) -> list[int]:
    """`snapshot_ids` 須是已完成的快照；`repositories` 取各 repo（名稱須唯一）最新的已完成快照。"""
    if dataset.snapshot_ids:
        found = set(
            session.scalars(
                select(Snapshot.id).where(
                    Snapshot.id.in_(dataset.snapshot_ids), Snapshot.status == "completed"
                )
            )
        )
        missing = [snapshot_id for snapshot_id in dataset.snapshot_ids if snapshot_id not in found]
        if missing:
            raise EvalConfigError(f"snapshots not found or not completed: {missing}")
        return list(dataset.snapshot_ids)
    result: list[int] = []
    for name in dataset.repositories or []:
        repository_ids = list(session.scalars(select(Repository.id).where(Repository.name == name)))
        if len(repository_ids) != 1:
            raise EvalConfigError(f"repository name {name!r} must match exactly one repository")
        snapshot_id = session.scalar(
            select(func.max(Snapshot.id)).where(
                Snapshot.repository_id == repository_ids[0], Snapshot.status == "completed"
            )
        )
        if snapshot_id is None:
            raise EvalConfigError(f"repository {name!r} has no completed snapshot")
        result.append(snapshot_id)
    return result


BackendOpener = Callable[[Dataset], AbstractContextManager[tuple[EvalBackend, list[int]]]]


@contextmanager
def open_db_backend(dataset: Dataset) -> Iterator[tuple[EvalBackend, list[int]]]:
    settings = Settings()
    engine = create_db_engine(settings)
    try:
        with create_session_factory(engine)() as session:
            snapshot_ids = resolve_snapshot_ids(session, dataset)
            session.rollback()
            profile = EmbeddingProfile.from_settings(settings)
            backend = DbEvalBackend(
                session,
                settings,
                snapshot_ids,
                lambda query: embed_query(settings, profile, query),
            )
            yield backend, snapshot_ids
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_qa_eval.py",
        description=(
            "Compare QA candidate models on one labelled question set (stage 4-5). "
            "Reports numbers only; it does not judge them against any threshold."
        ),
        epilog=(
            "Keys come ONLY from the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY); they are "
            "never printed or written. The --out file is a LOCAL result file: it contains the "
            "question and answer text, so do not commit or share it. It is created exclusively "
            "(an existing file is never overwritten). Repository chunks are sent only for "
            "repositories that have data-egress consent. --prices is a JSON object such as "
            '{"openai:gpt-6.2-sol": {"input_usd_per_million": "1.25", "output_usd_per_million": '
            '"10"}}; Anthropic models fall back to the default price table. The cost cap uses a '
            "worst-case estimate before each call (estimated input + --max-tokens at the output "
            "price) on top of the ACTUAL cost so far, so the run may stop slightly early.\n\n"
            "Exit codes: 0 completed, 2 refused or invalid input, 3 stopped by the cost cap.\n\n"
            + (evaluation.__doc__ or "")
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dataset", required=True, type=Path, help="dataset JSON (format below)")
    parser.add_argument(
        "--candidate",
        action="append",
        dest="candidates",
        required=True,
        metavar="PROVIDER:MODEL",
        help="repeatable, e.g. anthropic:claude-haiku-5-5 or openai:gpt-6.2-sol",
    )
    parser.add_argument(
        "--max-cost-usd",
        required=True,
        help="REQUIRED hard cap (USD) on the cumulative actual cost; the run stops before it",
    )
    parser.add_argument("--out", required=True, type=Path, help="raw result file (JSONL)")
    parser.add_argument("--prices", type=Path, help="JSON price table for non-default models")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"output token limit per call (default {DEFAULT_MAX_TOKENS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use a fake provider: no keys, no network, no cost (retrieval and consent still run)",
    )
    return parser


def _cap(raw: str) -> Decimal:
    try:
        cap = Decimal(raw)
    except InvalidOperation:
        raise EvalConfigError("--max-cost-usd must be a number") from None
    if not cap.is_finite() or cap <= 0:
        raise EvalConfigError("--max-cost-usd must be a positive finite number")
    return cap


def _read_json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EvalConfigError(f"{what} could not be read as JSON") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    backend_opener: BackendOpener = open_db_backend,
    provider_factory: ProviderFactory = default_provider_factory,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    environment: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(
        argv
    )  # 缺少必要參數（含 --max-cost-usd）時由 argparse 結束，代碼 2
    if environ is None:
        environ = os.environ
    sink: JsonlSink | None = None
    try:
        cap = _cap(args.max_cost_usd)
        if not 1 <= args.max_tokens <= MAX_TOKENS_CEILING:
            raise EvalConfigError(f"--max-tokens must be between 1 and {MAX_TOKENS_CEILING}")
        try:
            raw_dataset = args.dataset.read_text(encoding="utf-8")
        except OSError:
            raise EvalConfigError("dataset file could not be read") from None
        dataset = load_dataset(raw_dataset)
        candidates = parse_candidates(args.candidates)
        user_prices = _read_json(args.prices, "prices file") if args.prices else None
        prices = resolve_prices(candidates, user_prices, dry_run=args.dry_run)
        if args.out.exists():
            raise EvalConfigError("--out already exists; choose a new file")
        providers, secrets = build_providers(
            candidates, environ, dry_run=args.dry_run, factory=provider_factory
        )
        sink = JsonlSink(args.out, secrets)
        with backend_opener(dataset) as (backend, snapshot_ids):
            header = build_header(
                dataset=dataset,
                snapshot_ids=snapshot_ids,
                candidates=candidates,
                prices=prices,
                max_cost_usd=cap,
                max_tokens=args.max_tokens,
                dry_run=args.dry_run,
                now=now(),
                environment=environment if environment is not None else collect_environment(),
            )
            run = run_evaluation(
                dataset=dataset,
                candidates=candidates,
                providers=providers,
                prices=prices,
                backend=backend,
                max_cost_usd=cap,
                max_tokens=args.max_tokens,
                header=header,
                emit=sink.write,
                sleep=sleep,
            )
    except ConsentRefusedError as exc:
        err.write(f"refused: {exc} (nothing was sent to any provider)\n")
        return EXIT_CONFIG
    except EvalConfigError as exc:
        err.write(f"error: {exc}\n")
        return EXIT_CONFIG
    except RepoPilotError as exc:
        err.write(f"error: {exc.code}: {exc.message}\n")
        return EXIT_CONFIG
    finally:
        if sink is not None:
            sink.close()
    out.write(json.dumps(run.summary, indent=2, ensure_ascii=False) + "\n")
    out.write(f"results: {args.out}\n")
    if run.stop is not None:
        out.write(
            f"STOPPED by the cost cap before {run.stop.candidate} / {run.stop.item_id}: "
            f"spent {run.stop.cumulative_cost_usd} of {run.stop.max_cost_usd} USD\n"
        )
        return EXIT_COST_CAP
    return EXIT_OK
