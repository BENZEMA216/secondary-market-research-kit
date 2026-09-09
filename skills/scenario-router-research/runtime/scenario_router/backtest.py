"""Chronological historical replay for the frozen scenario-router strategy.

The strategy core remains authoritative.  This module supplies the missing
single-writer adapter that advances an exchange calendar, derives trailing
statistics from completed daily bars, batches simultaneous candidates, and
reuses :class:`PaperBroker` for portfolio accounting.

The fill model is intentionally limited: entries use supplied boundary NBBO,
exits use the supplied NBBO at the five-minute decision boundary, and fills are
all-or-reject.  There is no queue, partial-fill, auction, halt, impact, dividend,
or open-position corporate-action model.  Those limitations are emitted in
every run manifest and metrics file.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .calendar import ExchangeSession, TradingSessionCalendar
from .configuration import STRATEGY_VERSION, load_frozen_config
from .engine import EventSession, ReversalSession, SignalRouter, macd_open_signal
from .events import (
    ArticleLedger,
    EventLedger,
    EventQualification,
    FeedCoverage,
    ReferenceSnapshotLedger,
    choose_information_route,
    qualify_premarket_event,
)
from .indicators import Ema, MacdThreeWaveDetector, QuadStochasticDetector
from .models import (
    DailyBar,
    EntrySignal,
    EXCHANGE_TIMEZONE,
    IntradayBar,
    MacdSetup,
    require_aware,
    require_finite_number,
    require_nonempty_string,
    to_jsonable,
)
from .paper import CostModel, PaperBroker
from .portfolio import MarketMark, PortfolioLedger
from .portfolio_risk import RiskPolicy
from .risk import EntryQuote


BACKTEST_ENGINE_VERSION = "scenario-router-backtest-0.1.0"
OUTPUT_SCHEMA_VERSION = "scenario-router-backtest-output-1"
REQUIRED_DATA_FILES = (
    "calendar.csv",
    "universe.csv",
    "daily_bars.csv",
    "intraday_bars.csv",
    "quotes.csv",
    "feed_manifest.json",
    "articles.jsonl",
    "events.jsonl",
    "reference_snapshots.jsonl",
)


class BacktestDataError(ValueError):
    """The supplied replay dataset cannot support a causal result."""


class UnsupportedBacktestFeature(RuntimeError):
    """The replay encountered a deliberately unsupported market operation."""


@dataclass(frozen=True)
class SecuritySessionRecord:
    session: date
    ticker: str
    security_id: str
    price_basis_id: str
    security_classification: str
    sector: str
    cluster: str

    def __post_init__(self) -> None:
        for name in (
            "ticker", "security_id", "price_basis_id", "security_classification",
            "sector", "cluster",
        ):
            require_nonempty_string(getattr(self, name), name)


@dataclass(frozen=True)
class QuoteSnapshot:
    timestamp: datetime
    ticker: str
    security_id: str
    price_basis_id: str
    bid_price: float
    ask_price: float

    def __post_init__(self) -> None:
        require_aware(self.timestamp, "quote timestamp")
        for name in ("ticker", "security_id", "price_basis_id"):
            require_nonempty_string(getattr(self, name), name)
        require_finite_number(self.bid_price, "bid_price", positive=True)
        require_finite_number(self.ask_price, "ask_price", positive=True)
        if self.ask_price < self.bid_price:
            raise ValueError("ask_price cannot be below bid_price")

    @property
    def midpoint(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask_price - self.bid_price) / self.midpoint * 10_000.0


@dataclass(frozen=True)
class BacktestConfig:
    start_session: date
    end_session: date
    initial_cash: float = 100_000.0
    event_variant: str = "E2A"
    reversal_variant: str = "M4"
    event_mode: str = "strict_primary"
    coverage_mode: str = "retrospective_audit"
    data_mode: str = "historical_point_in_time"
    code_revision: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        if self.end_session < self.start_session:
            raise ValueError("end_session cannot precede start_session")
        require_finite_number(self.initial_cash, "initial_cash", positive=True)
        if self.event_variant not in {"E2A", "E2B"}:
            raise ValueError("event_variant must be E2A or E2B")
        if self.reversal_variant not in {"M2", "M3", "M4"}:
            raise ValueError("reversal_variant must be M2, M3, or M4")
        if self.event_mode not in {"strict_primary", "agent_assisted_secondary"}:
            raise ValueError("invalid event_mode")
        if self.coverage_mode not in {"retrospective_audit", "point_in_time"}:
            raise ValueError("coverage_mode must be retrospective_audit or point_in_time")
        if self.data_mode not in {"historical_point_in_time", "synthetic_fixture"}:
            raise ValueError("data_mode must be historical_point_in_time or synthetic_fixture")
        require_nonempty_string(self.code_revision, "code_revision")
        if (
            self.data_mode == "historical_point_in_time"
            and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.code_revision)
            is None
        ):
            raise ValueError(
                "historical_point_in_time requires a declared 40- or 64-hex immutable code revision"
            )

    @property
    def experiment_id(self) -> str:
        return (
            f"portfolio:{self.event_mode}:"
            f"{self.event_variant}+{self.reversal_variant}"
        )


@dataclass(frozen=True)
class BacktestDataset:
    calendar: TradingSessionCalendar
    universe: tuple[SecuritySessionRecord, ...]
    daily_bars: tuple[DailyBar, ...]
    intraday_bars: tuple[IntradayBar, ...]
    quotes: tuple[QuoteSnapshot, ...]
    article_ledger: ArticleLedger
    event_ledger: EventLedger
    reference_ledger: ReferenceSnapshotLedger
    input_hashes: Mapping[str, str]
    source_directory: str | None = None

    @classmethod
    def from_directory(cls, directory: str | Path) -> "BacktestDataset":
        root = Path(directory).resolve()
        if not root.is_dir():
            raise BacktestDataError(f"backtest data directory does not exist: {root}")
        missing = [name for name in REQUIRED_DATA_FILES if not (root / name).is_file()]
        if missing:
            raise BacktestDataError(f"backtest data files are missing: {', '.join(missing)}")

        hashes = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in REQUIRED_DATA_FILES
        }
        calendar_rows = _strict_csv(
            root / "calendar.csv", ("session", "market_open", "market_close")
        )
        calendar = TradingSessionCalendar(
            ExchangeSession(
                date.fromisoformat(row["session"]),
                _timestamp(row["market_open"], "market_open"),
                _timestamp(row["market_close"], "market_close"),
            )
            for row in calendar_rows
        )
        universe = tuple(
            SecuritySessionRecord(
                session=date.fromisoformat(row["session"]),
                ticker=row["ticker"],
                security_id=row["security_id"],
                price_basis_id=row["price_basis_id"],
                security_classification=row["security_classification"],
                sector=row["sector"],
                cluster=row["cluster"],
            )
            for row in _strict_csv(
                root / "universe.csv",
                (
                    "session", "ticker", "security_id", "price_basis_id",
                    "security_classification", "sector", "cluster",
                ),
            )
        )
        daily = tuple(
            DailyBar(
                ticker=row["ticker"],
                security_id=row["security_id"],
                price_basis_id=row["price_basis_id"],
                session=date.fromisoformat(row["session"]),
                open=_number(row["open"], "daily open"),
                high=_number(row["high"], "daily high"),
                low=_number(row["low"], "daily low"),
                close=_number(row["close"], "daily close"),
                volume=_number(row["volume"], "daily volume"),
            )
            for row in _strict_csv(
                root / "daily_bars.csv",
                (
                    "session", "ticker", "security_id", "price_basis_id",
                    "open", "high", "low", "close", "volume",
                ),
            )
        )
        intraday = tuple(
            IntradayBar(
                ticker=row["ticker"],
                security_id=row["security_id"],
                price_basis_id=row["price_basis_id"],
                start=_timestamp(row["start"], "intraday start"),
                open=_number(row["open"], "intraday open"),
                high=_number(row["high"], "intraday high"),
                low=_number(row["low"], "intraday low"),
                close=_number(row["close"], "intraday close"),
                volume=_number(row["volume"], "intraday volume"),
            )
            for row in _strict_csv(
                root / "intraday_bars.csv",
                (
                    "start", "ticker", "security_id", "price_basis_id",
                    "open", "high", "low", "close", "volume",
                ),
            )
        )
        quotes = tuple(
            QuoteSnapshot(
                timestamp=_timestamp(row["timestamp"], "quote timestamp"),
                ticker=row["ticker"],
                security_id=row["security_id"],
                price_basis_id=row["price_basis_id"],
                bid_price=_number(row["bid_price"], "bid_price"),
                ask_price=_number(row["ask_price"], "ask_price"),
            )
            for row in _strict_csv(
                root / "quotes.csv",
                (
                    "timestamp", "ticker", "security_id", "price_basis_id",
                    "bid_price", "ask_price",
                ),
            )
        )
        coverage = FeedCoverage.from_json(root / "feed_manifest.json")
        return cls(
            calendar=calendar,
            universe=universe,
            daily_bars=daily,
            intraday_bars=intraday,
            quotes=quotes,
            article_ledger=ArticleLedger.from_jsonl(root / "articles.jsonl", coverage),
            event_ledger=EventLedger.from_jsonl(root / "events.jsonl"),
            reference_ledger=ReferenceSnapshotLedger.from_jsonl(
                root / "reference_snapshots.jsonl"
            ),
            input_hashes=hashes,
            source_directory=str(root),
        )


@dataclass(frozen=True)
class _DailyDerived:
    prior_close: float | None
    average_volume_20: float | None
    median_dollar_volume_20: float | None
    ema10: float | None
    trailing_start: date | None
    trailing_end: date | None


@dataclass(frozen=True)
class BacktestResult:
    run_manifest: Mapping[str, Any]
    metrics: Mapping[str, Any]
    candidates: tuple[Mapping[str, Any], ...]
    orders: tuple[Mapping[str, Any], ...]
    fills: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    execution_audit: tuple[Mapping[str, Any], ...]
    daily_nav: tuple[Mapping[str, Any], ...]
    portfolio_snapshot: Mapping[str, Any]

    def write(self, directory: str | Path) -> Mapping[str, Any]:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        payloads: dict[str, bytes] = {
            "metrics.json": _json_bytes(self.metrics),
            "candidate_ledger.jsonl": _jsonl_bytes(self.candidates),
            "orders.jsonl": _jsonl_bytes(self.orders),
            "fills.jsonl": _jsonl_bytes(self.fills),
            "trades.jsonl": _jsonl_bytes(self.trades),
            "execution_audit.jsonl": _jsonl_bytes(self.execution_audit),
            "daily_nav.csv": _csv_bytes(self.daily_nav),
            "portfolio_snapshot.json": _json_bytes(self.portfolio_snapshot),
        }
        protected = set(payloads) | {"run_manifest.json"}
        existing = sorted(name for name in protected if (destination / name).exists())
        if existing:
            raise FileExistsError(
                "refusing to overwrite existing backtest artifacts: "
                + ", ".join(existing)
            )
        output_hashes: dict[str, str] = {}
        for name, payload in payloads.items():
            _atomic_write(destination / name, payload)
            output_hashes[name] = hashlib.sha256(payload).hexdigest()
        manifest = {**dict(self.run_manifest), "output_sha256": output_hashes}
        _atomic_write(destination / "run_manifest.json", _json_bytes(manifest))
        return manifest


def verify_backtest_output(directory: str | Path) -> Mapping[str, Any]:
    """Recompute artifact hashes and validate the restartable portfolio state."""

    root = Path(directory)
    try:
        manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestDataError(f"invalid run manifest in {root}: {exc}") from exc
    expected_names = {
        "metrics.json", "candidate_ledger.jsonl", "orders.jsonl", "fills.jsonl",
        "trades.jsonl", "execution_audit.jsonl", "daily_nav.csv",
        "portfolio_snapshot.json",
    }
    expected_hashes = manifest.get("output_sha256")
    if not isinstance(expected_hashes, Mapping) or set(expected_hashes) != expected_names:
        raise BacktestDataError("run manifest has an incomplete output hash set")
    actual_hashes: dict[str, str] = {}
    for name in sorted(expected_names):
        try:
            payload = (root / name).read_bytes()
        except OSError as exc:
            raise BacktestDataError(f"missing backtest artifact: {name}") from exc
        actual_hashes[name] = hashlib.sha256(payload).hexdigest()
        if actual_hashes[name] != expected_hashes[name]:
            raise BacktestDataError(f"backtest artifact hash mismatch: {name}")
    try:
        metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
        snapshot = json.loads(
            (root / "portfolio_snapshot.json").read_text(encoding="utf-8")
        )
        restored = PortfolioLedger.from_snapshot(snapshot)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise BacktestDataError(f"backtest artifact validation failed: {exc}") from exc
    if metrics.get("run_id") != manifest.get("run_id"):
        raise BacktestDataError("metrics and run manifest use different run IDs")
    return {
        "status": "PASS_BACKTEST_OUTPUT_INTEGRITY",
        "run_id": manifest["run_id"],
        "decision_result_sha256": manifest["decision_result_sha256"],
        "output_sha256": actual_hashes,
        "restored_final_equity": restored.equity,
    }


def simultaneous_entry_sort_key(
    signal: EntrySignal, quote: EntryQuote
) -> tuple[Any, ...]:
    """Frozen, order-independent priority for one execution timestamp."""

    if signal.signal_id == "" or quote.security_id != signal.security_id:
        raise ValueError("signal/quote identity mismatch")
    return (
        0 if signal.branch == "event" else 1,
        signal.execute_at,
        -quote.median_dollar_volume_20,
        signal.ticker,
        signal.variant,
        signal.signal_id,
    )


def _strict_csv(path: Path, expected_header: Sequence[str]) -> list[dict[str, str]]:
    payload = path.read_bytes()
    text = payload.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise BacktestDataError(f"{path.name}: CSV is empty")
    if tuple(rows[0]) != tuple(expected_header):
        raise BacktestDataError(
            f"{path.name}: header mismatch; expected={list(expected_header)}, actual={rows[0]}"
        )
    result: list[dict[str, str]] = []
    for line_number, row in enumerate(rows[1:], 2):
        if not row or all(not value.strip() for value in row):
            raise BacktestDataError(f"{path.name}:{line_number}: blank rows are not allowed")
        if len(row) != len(expected_header):
            raise BacktestDataError(
                f"{path.name}:{line_number}: expected {len(expected_header)} fields, got {len(row)}"
            )
        result.append(dict(zip(expected_header, row, strict=True)))
    return result


def _timestamp(raw: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BacktestDataError(f"{label} must be an ISO-8601 timestamp: {raw}") from exc
    require_aware(value, label)
    return value


def _number(raw: str, label: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise BacktestDataError(f"{label} must be numeric: {raw}") from exc
    require_finite_number(value, label, non_negative=label.endswith("volume"))
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(to_jsonable(dict(row)), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for row in rows
    ).encode("utf-8")


def _public_fill(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize a fill with unambiguous cost-accounting names.

    The portfolio ledger retains its historical ``fee`` and
    ``extra_slippage`` keys.  In backtest artifacts the fill price already
    contains spread, any policy-reference haircut, and extra slippage;
    commission alone is a separate cash deduction.
    """

    result = copy.deepcopy(dict(raw))
    result["schema_version"] = OUTPUT_SCHEMA_VERSION
    result["commission_cash_expense"] = result.pop("fee")
    result["extra_slippage_embedded_in_fill_price"] = result.pop(
        "extra_slippage"
    )
    return result


def _source_tree_sha256() -> str:
    """Hash the executable local strategy tree independently of a claimed SHA."""

    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "scenario_router").glob("*.py"))
    paths.extend((root / "frozen_config.json", root / "frozen_risk_policy.json"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _csv_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    if not rows:
        return b""
    stream = io.StringIO(newline="")
    fieldnames = list(rows[0])
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in fieldnames})
    return stream.getvalue().encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class HistoricalBacktester:
    """Replay one frozen E2 arm plus one frozen M2/M3/M4 arm.

    A runner owns exactly one portfolio.  Run every experiment and cost
    scenario in a fresh instance so detectors, risk state, and capital never
    leak between arms.
    """

    def __init__(
        self,
        dataset: BacktestDataset,
        config: BacktestConfig,
        cost_model: CostModel | None = None,
    ) -> None:
        self.dataset = dataset
        self.config = config
        self.costs = cost_model or CostModel()
        self._index_and_validate()
        self.strategy_config_hash, self.risk_policy_hash = self._frozen_hashes()
        self.source_tree_hash = _source_tree_sha256()
        self.dataset_id = "dataset-" + hashlib.sha256(
            _json_bytes(dict(sorted(self.dataset.input_hashes.items())))
        ).hexdigest()[:24]
        self.run_id = self._run_id()
        self.portfolio_id = self._portfolio_id()
        self.ledger = PortfolioLedger(config.initial_cash, self.portfolio_id)
        self.broker = PaperBroker(self.ledger, cost_model=self.costs)
        self.macd: dict[str, MacdThreeWaveDetector] = {}
        self.macd_last_session: dict[str, date] = {}
        self.quad: dict[str, QuadStochasticDetector] = {}
        self.quad_last_session: dict[str, date] = {}
        self.pending_setups: dict[str, MacdSetup] = {}
        self.qualifications: dict[str, EventQualification] = {}
        self.event_sessions: dict[str, EventSession] = {}
        self.reversal_sessions: dict[str, ReversalSession] = {}
        self.m2_setups: dict[str, MacdSetup] = {}
        self.candidates: list[dict[str, Any]] = []
        self.candidate_by_key: dict[tuple[date, str], dict[str, Any]] = {}
        self.execution_audit: list[dict[str, Any]] = []
        self.daily_nav: list[dict[str, Any]] = []
        self.order_ids_by_session: dict[date, list[str]] = defaultdict(list)
        self._current_candidate_start = 0
        self._previous_nav = config.initial_cash
        self._previous_fees = 0.0
        self._previous_extra_slippage = 0.0
        self._previous_realized = 0.0
        self._previous_unrealized = 0.0
        self._previous_fill_count = 0
        self._close_high_water = config.initial_cash
        self._has_run = False

    def _frozen_hashes(self) -> tuple[str, str]:
        root = Path(__file__).resolve().parents[1]
        _, strategy_hash = load_frozen_config(root / "frozen_config.json")
        risk_path = root / "frozen_risk_policy.json"
        risk_payload = risk_path.read_bytes()
        if json.loads(risk_payload) != asdict(RiskPolicy()):
            raise ValueError("frozen_risk_policy.json does not match the runtime policy")
        return strategy_hash, hashlib.sha256(risk_payload).hexdigest()

    def _run_id(self) -> str:
        identity = {
            "engine": BACKTEST_ENGINE_VERSION,
            "strategy": STRATEGY_VERSION,
            "strategy_config_sha256": self.strategy_config_hash,
            "risk_policy_sha256": self.risk_policy_hash,
            "source_tree_sha256": self.source_tree_hash,
            "config": to_jsonable(self.config),
            "cost_model": asdict(self.costs),
            "input_sha256": dict(sorted(self.dataset.input_hashes.items())),
        }
        return "backtest-" + hashlib.sha256(_json_bytes(identity)).hexdigest()[:24]

    def _portfolio_id(self) -> str:
        """Stable across mutations strictly after the configured sample end."""

        identity = {
            "engine": BACKTEST_ENGINE_VERSION,
            "strategy": STRATEGY_VERSION,
            "strategy_config_sha256": self.strategy_config_hash,
            "risk_policy_sha256": self.risk_policy_hash,
            "source_tree_sha256": self.source_tree_hash,
            "config": to_jsonable(self.config),
            "cost_model": asdict(self.costs),
        }
        return "backtest-portfolio-" + hashlib.sha256(_json_bytes(identity)).hexdigest()[:24]

    def _index_and_validate(self) -> None:
        calendar = self.dataset.calendar
        if self.config.start_session not in calendar.index:
            raise BacktestDataError("start_session is absent from calendar")
        if self.config.end_session not in calendar.index:
            raise BacktestDataError("end_session is absent from calendar")
        if calendar.index[self.config.start_session] == 0:
            raise BacktestDataError("calendar needs at least one session before start_session")
        sample_session_count = (
            calendar.index[self.config.end_session]
            - calendar.index[self.config.start_session]
            + 1
        )
        if self.config.coverage_mode == "point_in_time":
            if sample_session_count != 1:
                raise BacktestDataError(
                    "point_in_time replay with one feed_manifest.json supports exactly one "
                    "session; multi-session evidence requires a coverage-revision ledger"
                )
            if self.config.reversal_variant == "M4":
                raise BacktestDataError(
                    "M4 point_in_time replay requires coverage revisions at every intraday "
                    "decision boundary; one feed_manifest.json cannot supply them"
                )

        self.universe_by_session: dict[date, dict[str, SecuritySessionRecord]] = defaultdict(dict)
        tickers_by_session: dict[date, set[str]] = defaultdict(set)
        for record in self.dataset.universe:
            if record.session not in calendar.index:
                raise BacktestDataError(f"universe session is absent from calendar: {record.session}")
            existing = self.universe_by_session[record.session].get(record.security_id)
            if existing is not None:
                raise BacktestDataError(
                    f"duplicate universe identity: {record.session} {record.security_id}"
                )
            if record.ticker in tickers_by_session[record.session]:
                raise BacktestDataError(
                    f"duplicate point-in-time ticker: {record.session} {record.ticker}"
                )
            self.universe_by_session[record.session][record.security_id] = record
            tickers_by_session[record.session].add(record.ticker)

        for schedule in calendar.sessions:
            if (
                self.config.start_session <= schedule.session <= self.config.end_session
                and not self.universe_by_session.get(schedule.session)
            ):
                raise BacktestDataError(
                    f"point-in-time universe is empty for a replay session: {schedule.session}"
                )
            seconds = (schedule.market_close - schedule.market_open).total_seconds()
            if seconds % 300 != 0:
                raise BacktestDataError(
                    f"exchange session is not divisible into five-minute bars: {schedule.session}"
                )

        self.daily_by_key: dict[tuple[date, str], DailyBar] = {}
        for bar in self.dataset.daily_bars:
            key = (bar.session, bar.security_id)
            if bar.session not in calendar.index:
                raise BacktestDataError(f"daily bar session is absent from calendar: {key}")
            if key in self.daily_by_key:
                raise BacktestDataError(f"duplicate daily bar: {key}")
            self.daily_by_key[key] = bar

        intraday: dict[tuple[date, str], list[IntradayBar]] = defaultdict(list)
        for bar in self.dataset.intraday_bars:
            if bar.session not in calendar.index:
                raise BacktestDataError(
                    f"intraday bar session is absent from calendar: {bar.session}"
                )
            intraday[(bar.session, bar.security_id)].append(bar)
        self.intraday_by_key: dict[tuple[date, str], tuple[IntradayBar, ...]] = {}
        self.bar_by_start: dict[tuple[date, str, datetime], IntradayBar] = {}
        for key, values in intraday.items():
            ordered = tuple(sorted(values, key=lambda item: item.start))
            if len({item.start for item in ordered}) != len(ordered):
                raise BacktestDataError(f"duplicate intraday bar: {key}")
            self.intraday_by_key[key] = ordered
            for bar in ordered:
                self.bar_by_start[(bar.session, bar.security_id, bar.start)] = bar

        for session, security_id in self.daily_by_key:
            if security_id not in self.universe_by_session.get(session, {}):
                raise BacktestDataError(
                    f"daily bar has no point-in-time universe row: {session} {security_id}"
                )
        for session, security_id in self.intraday_by_key:
            if security_id not in self.universe_by_session.get(session, {}):
                raise BacktestDataError(
                    f"intraday bars have no point-in-time universe row: {session} {security_id}"
                )

        self.quote_by_key: dict[tuple[datetime, str], QuoteSnapshot] = {}
        for quote in self.dataset.quotes:
            key = (quote.timestamp, quote.security_id)
            if key in self.quote_by_key:
                raise BacktestDataError(f"duplicate quote: {quote.timestamp} {quote.security_id}")
            self.quote_by_key[key] = quote

        for session, records in self.universe_by_session.items():
            schedule = calendar.get(session)
            expected_starts: list[datetime] = []
            cursor = schedule.market_open
            while cursor < schedule.market_close:
                expected_starts.append(cursor)
                cursor += timedelta(minutes=5)
            for security_id, record in records.items():
                daily = self.daily_by_key.get((session, security_id))
                if daily is None:
                    raise BacktestDataError(f"missing daily bar: {session} {security_id}")
                if (
                    daily.ticker != record.ticker
                    or daily.price_basis_id != record.price_basis_id
                ):
                    raise BacktestDataError(
                        f"daily/universe identity mismatch: {session} {security_id}"
                    )
                bars = self.intraday_by_key.get((session, security_id), ())
                if [item.start for item in bars] != expected_starts:
                    raise BacktestDataError(
                        f"incomplete five-minute session: {session} {security_id}; "
                        f"expected={len(expected_starts)}, actual={len(bars)}"
                    )
                if any(
                    item.ticker != record.ticker
                    or item.price_basis_id != record.price_basis_id
                    for item in bars
                ):
                    raise BacktestDataError(
                        f"intraday/universe identity mismatch: {session} {security_id}"
                    )
                tolerance = max(daily.open, daily.high, daily.low, daily.close) * 1e-10
                intraday_high = max(item.high for item in bars)
                intraday_low = min(item.low for item in bars)
                intraday_volume = sum(item.volume for item in bars)
                volume_tolerance = max(1.0, intraday_volume * 1e-6)
                if (
                    abs(daily.open - bars[0].open) > tolerance
                    or abs(daily.close - bars[-1].close) > tolerance
                    or abs(daily.high - intraday_high) > tolerance
                    or abs(daily.low - intraday_low) > tolerance
                    or abs(daily.volume - intraday_volume) > volume_tolerance
                ):
                    raise BacktestDataError(
                        f"daily/intraday regular-hours OHLCV aggregate mismatch: "
                        f"{session} {security_id}"
                    )
        for (_, security_id), quote in self.quote_by_key.items():
            session = quote.timestamp.astimezone(EXCHANGE_TIMEZONE).date()
            if session not in calendar.index:
                raise BacktestDataError(f"quote session is absent from calendar: {quote.timestamp}")
            record = self.universe_by_session.get(session, {}).get(security_id)
            if record is None:
                raise BacktestDataError(
                    f"quote has no point-in-time universe row: {quote.timestamp} {security_id}"
                )
            if quote.ticker != record.ticker or quote.price_basis_id != record.price_basis_id:
                raise BacktestDataError(
                    f"quote/universe identity mismatch: {quote.timestamp} {security_id}"
                )
            schedule = calendar.get(session)
            local = quote.timestamp.astimezone(EXCHANGE_TIMEZONE)
            if (
                not schedule.market_open <= quote.timestamp <= schedule.market_close
                or local.minute % 5 != 0
                or local.second != 0
                or local.microsecond != 0
            ):
                raise BacktestDataError(
                    f"quote is not on a regular-session five-minute boundary: {quote.timestamp}"
                )

        self.derived: dict[tuple[date, str], _DailyDerived] = {}
        histories: dict[str, list[DailyBar]] = defaultdict(list)
        emas: dict[str, Ema] = {}
        ema_basis: dict[str, str] = {}
        last_seen: dict[str, date] = {}
        for schedule in calendar.sessions:
            session = schedule.session
            records = self.universe_by_session.get(session, {})
            for security_id, record in sorted(records.items()):
                bar = self.daily_by_key[(session, security_id)]
                prior_seen = last_seen.get(security_id)
                discontinuity = bool(
                    prior_seen is not None
                    and not calendar.is_next_session(prior_seen, session)
                )
                if discontinuity or ema_basis.get(security_id) != record.price_basis_id:
                    histories[security_id] = []
                    emas[security_id] = Ema(10)
                    ema_basis[security_id] = record.price_basis_id
                history = histories[security_id]
                previous_session = None
                try:
                    previous_session = calendar.previous(session).session
                except ValueError:
                    pass
                prior = history[-1] if history and history[-1].session == previous_session else None
                window = history[-20:] if len(history) >= 20 else []
                current_index = calendar.index[session]
                expected_sessions = [
                    item.session for item in calendar.sessions[
                        max(0, current_index - 20):current_index
                    ]
                ]
                complete_window = bool(
                    len(window) == 20
                    and prior is not None
                    and all(item.price_basis_id == record.price_basis_id for item in window)
                    and [item.session for item in window] == expected_sessions
                )
                ema10 = emas[security_id].update(bar.close)
                self.derived[(session, security_id)] = _DailyDerived(
                    prior_close=None if prior is None else prior.close,
                    average_volume_20=(
                        sum(item.volume for item in window) / 20.0 if complete_window else None
                    ),
                    median_dollar_volume_20=(
                        statistics.median(item.close * item.volume for item in window)
                        if complete_window else None
                    ),
                    ema10=ema10,
                    trailing_start=window[0].session if complete_window else None,
                    trailing_end=window[-1].session if complete_window else None,
                )
                history.append(bar)
                last_seen[security_id] = session

    def run(self) -> BacktestResult:
        if self._has_run:
            raise RuntimeError("HistoricalBacktester instances are single-use")
        self._has_run = True
        calendar = self.dataset.calendar
        for schedule in calendar.sessions:
            session = schedule.session
            if session > self.config.end_session:
                break
            if session < self.config.start_session:
                self._warm_intraday(session)
                self._update_daily_detectors(session)
                continue
            self._prepare_session(session)
            self._on_session_open(session)
            starts = self._session_starts(session)
            for start in starts:
                bars = {
                    security_id: self.bar_by_start[(session, security_id, start)]
                    for security_id in self.universe_by_session.get(session, {})
                }
                self._on_bar_boundary(
                    session,
                    bars,
                    start + timedelta(minutes=5),
                    start + timedelta(minutes=5) == schedule.market_close,
                )
            self._finalize_candidates(session)
            self._record_daily_nav(session)
            self._update_daily_detectors(session)

        trades = self._build_trades()
        metrics = self._build_metrics(trades)
        orders = tuple(
            {"schema_version": OUTPUT_SCHEMA_VERSION, **to_jsonable(order)}
            for order in sorted(
                self.ledger.orders.values(), key=lambda item: (item.submitted_at, item.order_id)
            )
        )
        fills = tuple(_public_fill(item) for item in self.ledger.fills)
        portfolio_snapshot = self.ledger.snapshot()
        semantic_metrics = {
            key: value for key, value in metrics.items() if key != "run_id"
        }
        semantic_payload = {
            "metrics": semantic_metrics,
            "candidates": [
                {key: value for key, value in item.items() if key != "run_id"}
                for item in self.candidates
            ],
            "orders": orders,
            "fills": fills,
            "trades": [
                {key: value for key, value in item.items() if key != "run_id"}
                for item in trades
            ],
            "execution_audit": self.execution_audit,
            "daily_nav": self.daily_nav,
            "portfolio_snapshot": portfolio_snapshot,
        }
        manifest = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "engine_version": BACKTEST_ENGINE_VERSION,
            "strategy_version": STRATEGY_VERSION,
            "declared_code_revision": self.config.code_revision,
            "code_revision_format_valid": re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.config.code_revision
            ) is not None,
            "strategy_config_sha256": self.strategy_config_hash,
            "risk_policy_sha256": self.risk_policy_hash,
            "source_tree_sha256": self.source_tree_hash,
            "dataset_id": self.dataset_id,
            "base_currency": "USD",
            "input_sha256": dict(sorted(self.dataset.input_hashes.items())),
            "data_inventory": {
                "calendar_sessions": len(self.dataset.calendar.sessions),
                "calendar_first_session": self.dataset.calendar.sessions[0].session.isoformat(),
                "calendar_last_session": self.dataset.calendar.sessions[-1].session.isoformat(),
                "universe_rows": len(self.dataset.universe),
                "daily_bars": len(self.dataset.daily_bars),
                "five_minute_bars": len(self.dataset.intraday_bars),
                "quote_snapshots": len(self.dataset.quotes),
                "article_records": len(self.dataset.article_ledger.records),
                "structured_event_records": len(self.dataset.event_ledger.records),
                "reference_snapshot_records": len(self.dataset.reference_ledger.records),
                "feed_provider": self.dataset.article_ledger.coverage.provider,
                "feed_manifest_captured_at": (
                    self.dataset.article_ledger.coverage.captured_at.isoformat()
                ),
            },
            "config": to_jsonable(self.config),
            "cost_model": asdict(self.costs),
            "coverage_semantics": (
                "Retrospective feed-completeness QA; manifest capture time may follow the decision."
                if self.config.coverage_mode == "retrospective_audit"
                else "Single-session only: the sole coverage manifest must exist and cover the pre-open decision cutoff."
            ),
            "execution_model": {
                "name": "FULL_FILL_IDEALIZED_BOUNDARY_NBBO_V1",
                "entry": "supplied boundary ask plus extra slippage",
                "exit": "minimum of supplied boundary bid and policy reference, minus extra slippage",
                "valuation_basis": (
                    "last completed five-minute bar close, except a deterministically "
                    "exiting position is marked at its projected fill; session open before "
                    "the first completed bar"
                ),
                "partial_fills": False,
                "queue_position": False,
                "market_impact": False,
                "m2_opening_assumption": "opening price observation and fill share one atomic timestamp",
                "intrabar_stop_assumption": "decision uses five-minute OHLC and boundary NBBO",
                "accounting_treatment": {
                    "spread": "embedded_in_fill_prices",
                    "policy_reference_haircut": "embedded_in_exit_fill_prices",
                    "extra_slippage": "embedded_in_fill_prices",
                    "commission": "separate_cash_deduction",
                },
            },
            "unsupported": [
                "open-position corporate actions",
                "ticker changes while a position is open",
                "dividends and cash interest",
                "halts and LULD",
                "auction/queue/partial-fill/market-impact simulation",
                "forced liquidation at the sample end",
                "out-of-core or partitioned replay for very large universes",
                "multi-session or intraday point-in-time coverage revision replay",
            ],
            "decision_result_sha256": hashlib.sha256(
                _json_bytes(semantic_payload)
            ).hexdigest(),
        }
        return BacktestResult(
            run_manifest=manifest,
            metrics=metrics,
            candidates=tuple(copy.deepcopy(self.candidates)),
            orders=orders,
            fills=fills,
            trades=tuple(trades),
            execution_audit=tuple(copy.deepcopy(self.execution_audit)),
            daily_nav=tuple(copy.deepcopy(self.daily_nav)),
            portfolio_snapshot=copy.deepcopy(portfolio_snapshot),
        )

    def _session_starts(self, session: date) -> tuple[datetime, ...]:
        schedule = self.dataset.calendar.get(session)
        result: list[datetime] = []
        cursor = schedule.market_open
        while cursor < schedule.market_close:
            result.append(cursor)
            cursor += timedelta(minutes=5)
        return tuple(result)

    def _macd_for(self, record: SecuritySessionRecord) -> MacdThreeWaveDetector:
        detector = self.macd.get(record.security_id)
        if detector is None:
            previous = self.dataset.calendar.previous(self.config.start_session).session
            detector = MacdThreeWaveDetector(
                record.ticker, record.security_id, not_before=previous
            )
            self.macd[record.security_id] = detector
        return detector

    def _quad_for(self, record: SecuritySessionRecord, session: date) -> QuadStochasticDetector:
        detector = self.quad.get(record.security_id)
        prior = self.quad_last_session.get(record.security_id)
        if detector is None or (
            prior is not None
            and prior != session
            and not self.dataset.calendar.is_next_session(prior, session)
        ):
            detector = QuadStochasticDetector(self.dataset.calendar)
            self.quad[record.security_id] = detector
        self.quad_last_session[record.security_id] = session
        return detector

    def _warm_intraday(self, session: date) -> None:
        if self.config.reversal_variant == "M2":
            return
        for security_id, record in sorted(self.universe_by_session.get(session, {}).items()):
            detector = self._quad_for(record, session)
            for bar in self.intraday_by_key[(session, security_id)]:
                detector.update(bar)

    def _update_daily_detectors(self, session: date) -> None:
        for security_id, record in sorted(self.universe_by_session.get(session, {}).items()):
            prior = self.macd_last_session.get(security_id)
            if (
                prior is not None
                and not self.dataset.calendar.is_next_session(prior, session)
            ):
                self.macd.pop(security_id, None)
            setup = self._macd_for(record).update(self.daily_by_key[(session, security_id)])
            self.macd_last_session[security_id] = session
            if setup is not None:
                self.pending_setups[security_id] = setup
        expired = [
            security_id for security_id, setup in self.pending_setups.items()
            if setup.signal_session < session
        ]
        for security_id in expired:
            del self.pending_setups[security_id]

    def _qualification(self, security_id: str, session: date) -> EventQualification:
        schedule = self.dataset.calendar.get(session)
        window_start = self.dataset.calendar.previous(session).market_close
        cutoff = schedule.market_open - timedelta(microseconds=1)
        if (
            self.config.coverage_mode == "point_in_time"
            and self.dataset.article_ledger.coverage.captured_at > cutoff
        ):
            return EventQualification(
                security_id=security_id,
                window_start=window_start,
                cutoff=cutoff,
                mode=self.config.event_mode,
                status="COVERAGE_UNKNOWN",
                eligible_variants=(),
                event_record_ids=(),
                reason_codes=("ABSTAIN_FUTURE_CAPTURED_COVERAGE_MANIFEST",),
            )
        return qualify_premarket_event(
            self.dataset.article_ledger,
            self.dataset.event_ledger,
            self.dataset.reference_ledger,
            security_id,
            session,
            self.dataset.calendar,
            mode=self.config.event_mode,
        )

    def _prepare_session(self, session: date) -> None:
        self._current_candidate_start = len(self.candidates)
        self.qualifications = {}
        self.event_sessions = {}
        self.reversal_sessions = {}
        self.m2_setups = {}
        for security_id, record in sorted(self.universe_by_session.get(session, {}).items()):
            qualification = self._qualification(security_id, session)
            self.qualifications[security_id] = qualification
            route = choose_information_route(qualification, self.config.event_variant)
            setup = self.pending_setups.get(security_id)
            if setup is not None and not self.dataset.calendar.is_next_session(
                setup.signal_session, session
            ):
                setup = None
            derived = self.derived[(session, security_id)]
            candidate = {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "sequence": len(self.candidates) + 1,
                "experiment_id": self.config.experiment_id,
                "session": session.isoformat(),
                "security_id": security_id,
                "ticker": record.ticker,
                "price_basis_id": record.price_basis_id,
                "security_classification": record.security_classification,
                "sector": record.sector,
                "cluster": record.cluster,
                "selected_event_variant": self.config.event_variant,
                "selected_reversal_variant": self.config.reversal_variant,
                "coverage_mode": self.config.coverage_mode,
                "qualification_window_start": qualification.window_start.isoformat(),
                "qualification_cutoff": qualification.cutoff.isoformat(),
                "qualification_mode": qualification.mode,
                "qualification_status": qualification.status,
                "eligible_variants": list(qualification.eligible_variants),
                "event_record_ids": list(qualification.event_record_ids),
                "qualification_reason_codes": list(qualification.reason_codes),
                "route": route.route,
                "route_reason": route.reason,
                "route_computed_at": route.computed_at.isoformat(),
                "macd_setup_signal_session": (
                    None if setup is None else setup.signal_session.isoformat()
                ),
                "macd_setup_present": setup is not None,
                "macd_third_wave_low": None if setup is None else setup.third_wave_low,
                "trailing_window_start": (
                    None if derived.trailing_start is None else derived.trailing_start.isoformat()
                ),
                "trailing_window_end": (
                    None if derived.trailing_end is None else derived.trailing_end.isoformat()
                ),
                "prior_close": derived.prior_close,
                "average_full_day_volume_20": derived.average_volume_20,
                "median_dollar_volume_20": derived.median_dollar_volume_20,
                "opening_gap": None,
                "opening_range_high": None,
                "opening_range_low": None,
                "opening_range_volume": None,
                "opening_volume_multiple_of_adv20": None,
                "session_engine_reason": None,
                "terminal_status": "PENDING",
                "terminal_reason": None,
                "terminal_at": None,
                "rejection_layer": None,
                "candidate_stage": "UNIVERSE_SESSION",
                "signal_id": None,
                "signal_detected_at": None,
                "signal_execute_at": None,
                "signal_stop_price": None,
                "signal_metadata": None,
                "order_id": None,
            }
            self.candidates.append(candidate)
            self.candidate_by_key[(session, security_id)] = candidate
            if route.route == "abstain":
                self._finish_candidate(candidate, "ABSTAIN", route.reason)
                continue
            if route.route == "event":
                if derived.prior_close is None or derived.average_volume_20 is None:
                    self._finish_candidate(
                        candidate, "DATA_REJECTED", "INSUFFICIENT_CAUSAL_TRAILING_20"
                    )
                    continue
                self.event_sessions[security_id] = EventSession(
                    record.ticker,
                    security_id,
                    session,
                    self.dataset.calendar,
                    prior_close=derived.prior_close,
                    average_full_day_volume_20=derived.average_volume_20,
                    prior_close_price_basis_id=self.daily_by_key[
                        (self.dataset.calendar.previous(session).session, security_id)
                    ].price_basis_id,
                    session_price_basis_id=record.price_basis_id,
                    event_qualification=qualification,
                )
                continue
            if setup is None:
                self._finish_candidate(candidate, "NO_SIGNAL", "NO_NEXT_SESSION_MACD_SETUP")
                continue
            if setup.price_basis_id != record.price_basis_id:
                self._finish_candidate(
                    candidate,
                    "NO_SIGNAL",
                    "INVALIDATED_SETUP_PRICE_BASIS_CHANGE",
                )
                continue
            if self.config.reversal_variant == "M2":
                self.m2_setups[security_id] = setup
            else:
                detector = self._quad_for(record, session)
                self.reversal_sessions[security_id] = ReversalSession(
                    setup,
                    session,
                    self.dataset.calendar,
                    record.price_basis_id,
                    detector,
                    self.dataset.article_ledger,
                    self.config.reversal_variant,
                )

    @staticmethod
    def _finish_candidate(
        candidate: dict[str, Any],
        status: str,
        reason: str,
        terminal_at: datetime | None = None,
    ) -> None:
        if candidate["terminal_status"] not in {"PENDING", status}:
            raise AssertionError("candidate received more than one terminal status")
        candidate["terminal_status"] = status
        candidate["terminal_reason"] = reason
        candidate["terminal_at"] = (
            terminal_at.isoformat()
            if terminal_at is not None else candidate["route_computed_at"]
        )
        candidate["rejection_layer"] = {
            "ABSTAIN": "INFORMATION_GATE",
            "DATA_REJECTED": "DATA_GATE",
            "NO_SIGNAL": "SIGNAL_EVALUATION",
            "ORDER_REJECTED": "ORDER_EVALUATION",
            "FILLED": None,
        }.get(status)
        candidate["candidate_stage"] = {
            "ABSTAIN": "INFORMATION_GATE",
            "DATA_REJECTED": "DATA_GATE",
            "NO_SIGNAL": "SIGNAL_EVALUATED_NO_ENTRY",
            "ORDER_REJECTED": "SUBMITTED_ENTRY_ORDER",
            "FILLED": "FILLED_ENTRY",
        }.get(status, status)

    def _on_session_open(self, session: date) -> None:
        schedule = self.dataset.calendar.get(session)
        opening = {
            security_id: self.bar_by_start[(session, security_id, schedule.market_open)]
            for security_id in self.universe_by_session.get(session, {})
        }
        self._mark_positions(
            session,
            schedule.market_open,
            {security_id: bar.open for security_id, bar in opening.items()},
        )
        for position_id in sorted(tuple(self.ledger.positions)):
            position = self.ledger.positions.get(position_id)
            if position is None:
                continue
            bar = opening.get(position.security_id)
            if bar is None:
                raise BacktestDataError(
                    f"open position has no opening bar: {position.security_id} {session}"
                )
            holding = self._holding_session(position.opened_at, session)
            if position.branch == "event":
                self._apply_exit(
                    position_id,
                    "on_session_open",
                    (bar.open, bar.price_basis_id),
                    {},
                    schedule.market_open,
                    bar.open,
                )
            else:
                self._apply_exit(
                    position_id,
                    "on_bar",
                    (bar.open, bar.open, bar.open, bar.open, bar.price_basis_id),
                    {"holding_session": holding, "session_close": False},
                    schedule.market_open,
                    bar.open,
                )

        signals: list[EntrySignal] = []
        for security_id, setup in sorted(self.m2_setups.items()):
            record = self.universe_by_session[session][security_id]
            signal = macd_open_signal(
                setup,
                session,
                self.dataset.calendar,
                opening[security_id].open,
                record.ticker,
                record.price_basis_id,
                "M2",
                self.dataset.article_ledger,
            )
            if signal is None:
                if opening[security_id].open <= setup.third_wave_low:
                    reason = "INVALIDATED_MACD_LOW_AT_OPEN"
                else:
                    news_start = self.dataset.calendar.news_window_start_for(
                        setup.third_wave_start_session
                    )
                    article_status = self.dataset.article_ledger.query(
                        security_id,
                        news_start,
                        schedule.market_open - timedelta(microseconds=1),
                    ).status
                    reason = f"M2_{article_status}_VETO"
                self._finish_candidate(
                    self.candidate_by_key[(session, security_id)],
                    "NO_SIGNAL",
                    reason,
                    schedule.market_open,
                )
            else:
                signals.append(signal)
        self._route_and_execute(signals, session, schedule.market_open)

    def _on_bar_boundary(
        self,
        session: date,
        bars: Mapping[str, IntradayBar],
        as_of: datetime,
        session_close: bool,
    ) -> None:
        self._mark_positions(
            session,
            as_of,
            self._boundary_mark_prices(session, bars, as_of, session_close),
        )
        for position_id in sorted(tuple(self.ledger.positions)):
            position = self.ledger.positions.get(position_id)
            if position is None:
                continue
            bar = bars.get(position.security_id)
            if bar is None:
                raise BacktestDataError(
                    f"open position has no synchronous bar: {position.security_id} {as_of}"
                )
            holding = self._holding_session(position.opened_at, session)
            if position.branch == "event":
                self._apply_exit(
                    position_id,
                    "on_intraday_bar",
                    (bar.open, bar.high, bar.low, bar.price_basis_id),
                    {},
                    as_of,
                    self._next_open_or_close(session, position.security_id, as_of),
                )
                if session_close and position_id in self.ledger.positions:
                    ema10 = self.derived[(session, position.security_id)].ema10
                    if ema10 is None:
                        raise BacktestDataError(
                            f"EMA10 is unavailable for an event exit: {session} {position.security_id}"
                        )
                    self._apply_exit(
                        position_id,
                        "on_session_close",
                        (bar.close, ema10, bar.price_basis_id),
                        {"holding_session": holding},
                        as_of,
                        bar.close,
                    )
            else:
                self._apply_exit(
                    position_id,
                    "on_bar",
                    (bar.open, bar.high, bar.low, bar.close, bar.price_basis_id),
                    {"holding_session": holding, "session_close": session_close},
                    as_of,
                    self._next_open_or_close(session, position.security_id, as_of),
                )

        signals: list[EntrySignal] = []
        for security_id, bar in sorted(bars.items()):
            reversal = self.reversal_sessions.get(security_id)
            if reversal is not None:
                signal = reversal.on_bar(bar)
                if signal is not None:
                    signals.append(signal)
            elif self.config.reversal_variant in {"M3", "M4"}:
                self._quad_for(self.universe_by_session[session][security_id], session).update(bar)
            event = self.event_sessions.get(security_id)
            if event is not None:
                signals.extend(event.on_bar(bar))
        self._route_and_execute(signals, session, as_of)

    def _boundary_mark_prices(
        self,
        session: date,
        bars: Mapping[str, IntradayBar],
        as_of: datetime,
        session_close: bool,
    ) -> dict[str, float]:
        """Return a synchronous, path-consistent mark for the bar boundary.

        If the completed bar deterministically causes an exit, marking that
        position at the bar close immediately before filling at a worse policy
        reference can invent an unattainable equity peak and false drawdown.
        Preview the immutable exit policy and mark that position at the exact
        projected fill price instead.  Entry risk still sees one synchronous
        portfolio mark for the timestamp.
        """

        prices = {security_id: bar.close for security_id, bar in bars.items()}
        for position_id in sorted(self.ledger.positions):
            position = self.ledger.positions[position_id]
            record = self.universe_by_session.get(session, {}).get(
                position.security_id
            )
            # Preserve _mark_positions' explicit identity/corporate-action
            # errors instead of allowing a policy ValueError to mask them.
            if (
                record is None
                or record.ticker != position.ticker
                or record.price_basis_id != position.price_basis_id
            ):
                continue
            bar = bars.get(position.security_id)
            if bar is None:
                continue
            holding = self._holding_session(position.opened_at, session)
            policy = copy.deepcopy(self.broker.get_exit_policy(position_id))
            decisions: tuple[Any, ...] = ()
            if position.branch == "event":
                outcome = policy.on_intraday_bar(
                    bar.open, bar.high, bar.low, bar.price_basis_id
                )
                if outcome is not None:
                    decisions = (outcome,)
                elif session_close:
                    ema10 = self.derived[(session, position.security_id)].ema10
                    if ema10 is None:
                        raise BacktestDataError(
                            f"EMA10 is unavailable for an event exit: "
                            f"{session} {position.security_id}"
                        )
                    decisions = policy.on_session_close(
                        bar.close,
                        ema10,
                        bar.price_basis_id,
                        holding_session=holding,
                    )
            else:
                outcome = policy.on_bar(
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.price_basis_id,
                    holding_session=holding,
                    session_close=session_close,
                )
                if outcome is not None:
                    decisions = (outcome,)
            if decisions:
                next_open = self._next_open_or_close(
                    session, position.security_id, as_of
                )
                quote = self._quote(
                    position.security_id,
                    as_of,
                    next_open,
                    require_trailing=False,
                )
                decision = decisions[0]
                projected_fill = self.costs.sell_price(quote, decision.price)
                shares = (
                    position.quantity
                    if decision.action == "exit"
                    else min(
                        position.quantity,
                        max(
                            1,
                            math.floor(
                                position.initial_quantity * decision.fraction
                            ),
                        ),
                    )
                )
                # PaperBroker marks any surviving shares at boundary bid after
                # the partial fill.  This weighted mark makes pre/post values
                # identical before commission and applies execution slippage
                # only to the shares actually sold.
                prices[position.security_id] = (
                    shares * projected_fill
                    + (position.quantity - shares) * quote.bid_price
                ) / position.quantity
        return prices

    def _mark_positions(
        self,
        session: date,
        as_of: datetime,
        prices: Mapping[str, float],
    ) -> None:
        marks: list[MarketMark] = []
        for position in self.ledger.positions.values():
            record = self.universe_by_session.get(session, {}).get(position.security_id)
            if record is None:
                raise BacktestDataError(
                    f"open position left the point-in-time universe: {position.security_id} {session}"
                )
            if record.ticker != position.ticker:
                raise UnsupportedBacktestFeature(
                    f"ticker change while position is open: {position.ticker} -> {record.ticker}"
                )
            if record.price_basis_id != position.price_basis_id:
                raise UnsupportedBacktestFeature(
                    f"open-position corporate action requires atomic rebase: "
                    f"{position.security_id} {position.price_basis_id} -> {record.price_basis_id}"
                )
            if position.security_id not in prices:
                raise BacktestDataError(
                    f"missing synchronous mark: {position.security_id} {as_of}"
                )
            marks.append(
                MarketMark(position.security_id, prices[position.security_id], as_of, record.price_basis_id)
            )
        self.broker.on_market(marks, as_of)

    def _apply_exit(
        self,
        position_id: str,
        method: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        as_of: datetime,
        next_bar_open: float,
    ) -> None:
        if position_id not in self.ledger.positions:
            return
        policy = self.broker.get_exit_policy(position_id)
        preview = copy.deepcopy(policy)
        outcome = getattr(preview, method)(*args, **dict(kwargs))
        decisions = outcome if isinstance(outcome, tuple) else (() if outcome is None else (outcome,))
        quote = None
        if decisions:
            position = self.ledger.positions[position_id]
            quote = self._quote(position.security_id, as_of, next_bar_open, require_trailing=False)
        result = self.broker.propose_and_execute_exit(
            position_id,
            policy,
            method,
            *args,
            quote=quote,
            as_of=as_of,
            **dict(kwargs),
        )
        if result.status == "REJECTED":
            raise BacktestDataError(
                f"exit could not be replayed: {position_id} {as_of} {result.reason}"
            )
        if result.fills:
            assert quote is not None
            for index, fill in enumerate(result.fills):
                decision = decisions[index]
                midpoint = (quote.bid_price + quote.ask_price) / 2.0
                executable_before_extra_slippage = min(
                    quote.bid_price, decision.price
                )
                self.execution_audit.append({
                    "schema_version": OUTPUT_SCHEMA_VERSION,
                    "kind": "EXIT",
                    "fill_id": fill["fill_id"],
                    "position_id": position_id,
                    "at": as_of.isoformat(),
                    "method": method,
                    "reason": fill["reason"],
                    "bid": quote.bid_price,
                    "ask": quote.ask_price,
                    "midpoint": midpoint,
                    "spread_bps": quote.spread_bps,
                    "policy_reference_price": decision.price,
                    "fill_price": fill["price"],
                    "quantity": fill["quantity"],
                    "spread_crossing_cost": fill["quantity"] * (
                        midpoint - quote.bid_price
                    ),
                    "policy_reference_haircut_embedded_in_fill_price": (
                        fill["quantity"]
                        * (quote.bid_price - executable_before_extra_slippage)
                    ),
                    "extra_slippage_embedded_in_fill_price": fill[
                        "extra_slippage"
                    ],
                    "commission_cash_expense": fill["fee"],
                    "fill_model": "FULL_FILL_IDEALIZED_BOUNDARY_NBBO_V1",
                })

    def _route_and_execute(
        self, signals: Sequence[EntrySignal], session: date, as_of: datetime
    ) -> None:
        if not signals:
            return
        routed = SignalRouter.select(
            signals,
            event_variant=self.config.event_variant,
            reversal_variant=self.config.reversal_variant,
            qualifications_by_security_id=self.qualifications,
            event_mode=self.config.event_mode,
            calendar=self.dataset.calendar,
        )
        quote_by_signal: dict[str, EntryQuote] = {}
        for signal in routed:
            next_open = self._next_open_or_close(session, signal.security_id, as_of)
            quote_by_signal[signal.signal_id] = self._quote(
                signal.security_id, as_of, next_open, require_trailing=True
            )
        ordered = sorted(
            routed,
            key=lambda signal: simultaneous_entry_sort_key(
                signal, quote_by_signal[signal.signal_id]
            ),
        )
        for signal in ordered:
            candidate = self.candidate_by_key[(session, signal.security_id)]
            if candidate["terminal_status"] != "PENDING":
                raise AssertionError("a terminal candidate emitted another signal")
            quote = quote_by_signal[signal.signal_id]
            record = self.universe_by_session[session][signal.security_id]
            order = self.broker.submit_and_fill(
                signal,
                quote,
                sector=record.sector,
                cluster=record.cluster,
                as_of=as_of,
            )
            candidate["signal_id"] = signal.signal_id
            candidate["signal_detected_at"] = signal.detected_at.isoformat()
            candidate["signal_execute_at"] = signal.execute_at.isoformat()
            candidate["signal_stop_price"] = signal.stop_price
            candidate["signal_metadata"] = to_jsonable(signal.metadata)
            candidate["order_id"] = order.order_id
            self.order_ids_by_session[session].append(order.order_id)
            self._finish_candidate(
                candidate,
                "FILLED" if order.status == "FILLED" else "ORDER_REJECTED",
                order.reason,
                as_of,
            )
            entry_fill = None
            if order.status == "FILLED":
                entry_fill = next(
                    item
                    for item in reversed(self.ledger.fills)
                    if item.get("order_id") == order.order_id
                    and item["side"] == "buy"
                )
            midpoint = (quote.bid_price + quote.ask_price) / 2.0
            self.execution_audit.append({
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "kind": "ENTRY",
                "fill_id": None if entry_fill is None else entry_fill["fill_id"],
                "signal_id": signal.signal_id,
                "order_id": order.order_id,
                "at": as_of.isoformat(),
                "ticker": signal.ticker,
                "security_id": signal.security_id,
                "branch": signal.branch,
                "variant": signal.variant,
                "bid": quote.bid_price,
                "ask": quote.ask_price,
                "midpoint": midpoint,
                "spread_bps": quote.spread_bps,
                "next_bar_open": quote.next_bar_open,
                "stop_price": signal.stop_price,
                "status": order.status,
                "reason": order.reason,
                "quantity": order.quantity,
                "fill_price": order.estimated_entry if order.status == "FILLED" else None,
                "spread_crossing_cost": (
                    order.quantity * (quote.ask_price - midpoint)
                    if order.status == "FILLED" else 0.0
                ),
                "policy_reference_haircut_embedded_in_fill_price": 0.0,
                "extra_slippage_embedded_in_fill_price": (
                    entry_fill["extra_slippage"] if entry_fill is not None else 0.0
                ),
                "commission_cash_expense": (
                    order.estimated_fee if order.status == "FILLED" else 0.0
                ),
                "fill_model": "FULL_FILL_IDEALIZED_BOUNDARY_NBBO_V1",
            })

    def _quote(
        self,
        security_id: str,
        as_of: datetime,
        next_bar_open: float,
        *,
        require_trailing: bool,
    ) -> EntryQuote:
        snapshot = self.quote_by_key.get((as_of, security_id))
        if snapshot is None:
            raise BacktestDataError(f"missing boundary NBBO: {security_id} {as_of}")
        session = as_of.astimezone(EXCHANGE_TIMEZONE).date()
        record = self.universe_by_session.get(session, {}).get(security_id)
        if record is None:
            raise BacktestDataError(f"quote has no point-in-time universe record: {security_id} {as_of}")
        if (
            snapshot.ticker != record.ticker
            or snapshot.price_basis_id != record.price_basis_id
        ):
            raise BacktestDataError(f"quote/universe identity mismatch: {security_id} {as_of}")
        derived = self.derived[(session, security_id)]
        if require_trailing and (
            derived.prior_close is None or derived.median_dollar_volume_20 is None
        ):
            raise BacktestDataError(
                f"entry has fewer than 20 causal trailing daily bars: {security_id} {session}"
            )
        prior_close = derived.prior_close
        if prior_close is None:
            prior_close = self.daily_by_key[(session, security_id)].open
        median_dollars = derived.median_dollar_volume_20 or 0.0
        return EntryQuote(
            ticker=record.ticker,
            security_id=security_id,
            price_basis_id=record.price_basis_id,
            timestamp=as_of,
            next_bar_open=next_bar_open,
            bid_price=snapshot.bid_price,
            ask_price=snapshot.ask_price,
            prior_close=prior_close,
            median_dollar_volume_20=median_dollars,
            security_classification=record.security_classification,
        )

    def _next_open_or_close(self, session: date, security_id: str, as_of: datetime) -> float:
        schedule = self.dataset.calendar.get(session)
        if as_of == schedule.market_open:
            return self.bar_by_start[(session, security_id, as_of)].open
        if as_of < schedule.market_close:
            bar = self.bar_by_start.get((session, security_id, as_of))
            if bar is None:
                raise BacktestDataError(
                    f"signal boundary has no next five-minute bar: {security_id} {as_of}"
                )
            return bar.open
        return self.daily_by_key[(session, security_id)].close

    def _holding_session(self, opened_at: datetime, session: date) -> int:
        opened_session = opened_at.astimezone(EXCHANGE_TIMEZONE).date()
        return (
            self.dataset.calendar.index[session]
            - self.dataset.calendar.index[opened_session]
            + 1
        )

    def _finalize_candidates(self, session: date) -> None:
        for security_id in sorted(self.universe_by_session.get(session, {})):
            candidate = self.candidate_by_key[(session, security_id)]
            event = self.event_sessions.get(security_id)
            reversal = self.reversal_sessions.get(security_id)
            if event is not None:
                candidate["session_engine_reason"] = event.reason
                first = event.opening_bars.get(event.OPENING_STARTS[0])
                if first is not None:
                    candidate["opening_gap"] = first.open / event.prior_close - 1.0
                if event.opening_range is not None:
                    candidate["opening_range_high"] = event.opening_range.high
                    candidate["opening_range_low"] = event.opening_range.low
                    candidate["opening_range_volume"] = event.opening_range.volume
                    candidate["opening_volume_multiple_of_adv20"] = (
                        event.opening_range.volume / event.adv20
                    )
                reason = event.reason
            elif reversal is not None:
                candidate["session_engine_reason"] = reversal.reason
                reason = reversal.reason
                if reason == "ACTIVE":
                    reason = "NO_QUAD_STOCHASTIC_TRIGGER"
            elif security_id in self.m2_setups:
                reason = "NO_M2_SIGNAL"
            else:
                reason = "NO_SIGNAL"
            if candidate["terminal_status"] != "PENDING":
                continue
            self._finish_candidate(
                candidate, "NO_SIGNAL", reason, self.dataset.calendar.get(session).market_close
            )

    def _record_daily_nav(self, session: date) -> None:
        schedule = self.dataset.calendar.get(session)
        if self.ledger.last_event_at != schedule.market_close:
            raise AssertionError("daily NAV requires a closing portfolio mark")
        begin_equity = self._previous_nav
        end_equity = self.ledger.equity
        commission_today = self.ledger.fees_paid - self._previous_fees
        slippage_today = self.ledger.slippage_paid - self._previous_extra_slippage
        realized_today = self.ledger.realized_gross_pnl - self._previous_realized
        unrealized_end = self.ledger.unrealized_pnl
        unrealized_change = unrealized_end - self._previous_unrealized
        net_pnl_today = end_equity - begin_equity
        reconciliation_residual = net_pnl_today - (
            realized_today + unrealized_change - commission_today
        )
        session_fills = self.ledger.fills[self._previous_fill_count:]
        partial_exit_at_close = any(
            item["side"] == "sell"
            and item["position_id"] in self.ledger.positions
            and item["at"] == schedule.market_close.isoformat()
            for item in session_fills
        )
        session_candidates = self.candidates[self._current_candidate_start:]
        session_orders = [
            self.ledger.orders[order_id]
            for order_id in self.order_ids_by_session.get(session, ())
        ]
        self._close_high_water = max(self._close_high_water, end_equity)
        row = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "session": session.isoformat(),
            "cash": self.ledger.cash,
            "market_value": end_equity - self.ledger.cash,
            "begin_equity": begin_equity,
            "end_equity": end_equity,
            "equity": end_equity,
            "net_pnl_today": net_pnl_today,
            "daily_return": end_equity / begin_equity - 1.0,
            "intraday_mark_high_water": self.ledger.high_water_mark,
            "intraday_mark_drawdown": self.ledger.drawdown,
            "maximum_intraday_mark_drawdown": self.ledger.maximum_drawdown,
            "close_high_water": self._close_high_water,
            "close_drawdown": (
                (self._close_high_water - end_equity) / self._close_high_water
            ),
            "gross_exposure_fraction": (
                (end_equity - self.ledger.cash) / end_equity
                if end_equity > 0 else None
            ),
            "position_count": len(self.ledger.positions),
            "pending_order_count": len(self.ledger.pending_orders),
            "open_initial_stop_risk_dollars": sum(
                position.initial_stop_risk for position in self.ledger.positions.values()
            ),
            "open_initial_stop_risk_fraction": (
                sum(
                    position.initial_stop_risk
                    for position in self.ledger.positions.values()
                ) / end_equity
                if end_equity > 0 else None
            ),
            "realized_fill_pnl_before_commission_today": realized_today,
            "unrealized_fill_basis_pnl_begin": self._previous_unrealized,
            "unrealized_fill_basis_pnl_end": unrealized_end,
            "unrealized_fill_basis_pnl_change_today": unrealized_change,
            "commission_cash_expense_today": commission_today,
            "extra_slippage_embedded_in_fill_prices_today": slippage_today,
            "commission_cash_expense_cumulative": self.ledger.fees_paid,
            "extra_slippage_embedded_in_fill_prices_cumulative": (
                self.ledger.slippage_paid
            ),
            "pnl_reconciliation_residual": reconciliation_residual,
            "candidate_count": len(session_candidates),
            "signal_count": sum(item["signal_id"] is not None for item in session_candidates),
            "entry_order_count": len(session_orders),
            "filled_entry_order_count": sum(
                item.status == "FILLED" for item in session_orders
            ),
            "entry_order_rejection_count": sum(
                item.status == "REJECTED" for item in session_orders
            ),
            "entry_fill_count": sum(
                item["side"] == "buy" for item in session_fills
            ),
            "exit_fill_count": sum(
                item["side"] == "sell" for item in session_fills
            ),
            "total_fill_count": len(session_fills),
            "risk_state": self.broker.risk.refresh(self.ledger, schedule.market_close),
            "mark_at": schedule.market_close.isoformat(),
            "valuation_basis": (
                "mixed_completed_bar_close_and_boundary_bid_after_partial_exit"
                if partial_exit_at_close
                else "last_completed_five_minute_bar_close"
            ),
        }
        self.daily_nav.append(row)
        self._previous_nav = end_equity
        self._previous_fees = self.ledger.fees_paid
        self._previous_extra_slippage = self.ledger.slippage_paid
        self._previous_realized = self.ledger.realized_gross_pnl
        self._previous_unrealized = unrealized_end
        self._previous_fill_count = len(self.ledger.fills)

    def _build_trades(self) -> list[dict[str, Any]]:
        by_position: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fill in self.ledger.fills:
            by_position[fill["position_id"]].append(fill)
        audit_by_fill_id = {
            item["fill_id"]: item
            for item in self.execution_audit
            if item.get("fill_id") is not None
        }
        candidate_by_signal = {
            item["signal_id"]: item
            for item in self.candidates
            if item["signal_id"] is not None
        }
        trades: list[dict[str, Any]] = []
        for order in sorted(
            (item for item in self.ledger.orders.values() if item.status == "FILLED"),
            key=lambda item: (item.filled_at, item.order_id),
        ):
            fills = by_position[order.order_id]
            entry = next(item for item in fills if item["side"] == "buy")
            exits = [item for item in fills if item["side"] == "sell"]
            sold = sum(item["quantity"] for item in exits)
            realized_gross = sum(
                item["quantity"] * (item["price"] - entry["price"]) for item in exits
            )
            fees = sum(item["fee"] for item in fills)
            position = self.ledger.positions.get(order.order_id)
            unrealized = (
                0.0
                if position is None
                else position.quantity * (position.mark_price - position.entry_price)
            )
            last_exit = exits[-1] if exits else None
            opened_at = datetime.fromisoformat(entry["at"])
            holding_sessions = None
            if position is None and last_exit is not None:
                exit_at = datetime.fromisoformat(last_exit["at"])
                holding_sessions = self._holding_session(
                    opened_at, exit_at.astimezone(EXCHANGE_TIMEZONE).date()
                )
            signal = order.signal
            candidate = candidate_by_signal.get(signal["signal_id"])
            net_pnl = realized_gross + unrealized - fees
            spread_crossing_cost = sum(
                audit_by_fill_id[item["fill_id"]]["spread_crossing_cost"]
                for item in fills
            )
            policy_reference_haircut = sum(
                audit_by_fill_id[item["fill_id"]][
                    "policy_reference_haircut_embedded_in_fill_price"
                ]
                for item in fills
            )
            trades.append({
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "experiment_id": self.config.experiment_id,
                "trade_id": order.order_id,
                "signal_id": signal["signal_id"],
                "context_key": order.context_key,
                "security_id": signal["security_id"],
                "ticker": signal["ticker"],
                "branch": signal["branch"],
                "variant": signal["variant"],
                "information_route_reason": (
                    None if candidate is None else candidate["route_reason"]
                ),
                "qualification_status": (
                    None if candidate is None else candidate["qualification_status"]
                ),
                "event_record_ids": (
                    [] if candidate is None else candidate["event_record_ids"]
                ),
                "status": "CLOSED" if position is None else "OPEN",
                "entry_at": entry["at"],
                "entry_fill_id": entry["fill_id"],
                "entry_price": entry["price"],
                "entry_notional": entry["quantity"] * entry["price"],
                "entry_commission_cash_expense": entry["fee"],
                "entry_extra_slippage_embedded_in_fill_price": entry[
                    "extra_slippage"
                ],
                "initial_quantity": order.quantity,
                "remaining_quantity": order.quantity - sold,
                "initial_stop": signal["stop_price"],
                "initial_price_stop_risk_dollars": order.initial_stop_risk,
                "exit_fills": [_public_fill(item) for item in exits],
                "last_exit_at": None if last_exit is None else last_exit["at"],
                "last_exit_reason": None if last_exit is None else last_exit["reason"],
                "final_exit_at": (
                    last_exit["at"]
                    if position is None and last_exit is not None else None
                ),
                "final_exit_reason": (
                    last_exit["reason"]
                    if position is None and last_exit is not None else None
                ),
                "holding_sessions": holding_sessions,
                "open_holding_sessions": (
                    self._holding_session(opened_at, self.config.end_session)
                    if position is not None else None
                ),
                "mark_at": None if position is None else position.mark_at.isoformat(),
                "mark_price": None if position is None else position.mark_price,
                "mark_valuation_basis": (
                    None if position is None
                    else (
                        "boundary_nbbo_bid_after_partial_exit"
                        if last_exit is not None
                        and last_exit["at"] == position.mark_at.isoformat()
                        else "last_completed_five_minute_bar_close"
                    )
                ),
                "realized_fill_pnl_before_commission": realized_gross,
                "unrealized_fill_basis_pnl": unrealized,
                "commission_cash_expense": fees,
                "spread_crossing_cost_embedded_in_fill_prices": spread_crossing_cost,
                "policy_reference_haircut_embedded_in_exit_fill_prices": (
                    policy_reference_haircut
                ),
                "extra_slippage_embedded_in_fill_prices": sum(
                    item["extra_slippage"] for item in fills
                ),
                "net_pnl_after_commission": net_pnl,
                "pnl_reconciliation_residual": net_pnl - (
                    realized_gross + unrealized - fees
                ),
                "net_r_multiple": (
                    net_pnl / order.initial_stop_risk
                    if order.initial_stop_risk > 0 else None
                ),
                "accounting_treatment": {
                    "spread": "embedded_in_fill_prices",
                    "policy_reference_haircut": "embedded_in_exit_fill_prices",
                    "extra_slippage": "embedded_in_fill_prices",
                    "commission": "separate_cash_deduction",
                },
            })
        return trades

    def _build_metrics(self, trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        returns = [float(item["daily_return"]) for item in self.daily_nav]
        total_return = self.ledger.equity / self.config.initial_cash - 1.0
        periods = len(returns)
        cagr = (1.0 + total_return) ** (252.0 / periods) - 1.0 if periods else None
        volatility = statistics.stdev(returns) * math.sqrt(252.0) if len(returns) > 1 else None
        sharpe = None
        if len(returns) > 1:
            deviation = statistics.stdev(returns)
            if deviation > 0:
                sharpe = statistics.mean(returns) / deviation * math.sqrt(252.0)
        downside = [min(0.0, value) for value in returns]
        downside_deviation = (
            math.sqrt(sum(value * value for value in downside) / len(downside))
            if downside else 0.0
        )
        sortino = (
            statistics.mean(returns) / downside_deviation * math.sqrt(252.0)
            if downside_deviation > 0 else None
        )
        closed = [item for item in trades if item["status"] == "CLOSED"]
        closed_net = [float(item["net_pnl_after_commission"]) for item in closed]
        profits = sum(max(0.0, value) for value in closed_net)
        losses = -sum(min(0.0, value) for value in closed_net)
        terminal_counts = Counter(item["terminal_status"] for item in self.candidates)
        terminal_reasons = Counter(item["terminal_reason"] for item in self.candidates)
        routes = Counter(item["route"] for item in self.candidates)
        branch_pnl: dict[str, float] = defaultdict(float)
        for item in trades:
            branch_pnl[f"{item['branch']}:{item['variant']}"] += float(
                item["net_pnl_after_commission"]
            )
        average_equity = statistics.mean(
            [self.config.initial_cash] + [float(item["equity"]) for item in self.daily_nav]
        )
        turnover = sum(
            fill["quantity"] * fill["price"] for fill in self.ledger.fills
        ) / average_equity
        warnings = [
            "Metrics are descriptive and contain no multiple-testing adjustment or confidence interval.",
            "No benchmark, dividend, cash-interest, factor, or capacity attribution is included.",
            "Open trades are marked, not force-liquidated, at the sample end.",
            "A successful replay does not certify source completeness or profitability.",
        ]
        if self.config.coverage_mode == "retrospective_audit":
            warnings.append(
                "The no-article branch uses an after-the-fact feed-completeness certificate, not live-known coverage state."
            )
        if re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.config.code_revision
        ) is None:
            warnings.append(
                "The caller did not declare a 40- or 64-hex immutable code revision."
            )
        portfolio_net_pnl = self.ledger.equity - self.config.initial_cash
        trade_bookkeeping_net_pnl = sum(
            float(item["net_pnl_after_commission"]) for item in trades
        )
        daily_bookkeeping_net_pnl = sum(
            float(item["net_pnl_today"]) for item in self.daily_nav
        )
        close_drawdowns = [float(item["close_drawdown"]) for item in self.daily_nav]
        signals = sum(item["signal_id"] is not None for item in self.candidates)
        submitted_orders = sum(item["order_id"] is not None for item in self.candidates)
        filled_entries = sum(
            item["terminal_status"] == "FILLED" for item in self.candidates
        )
        return {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "research_status": (
                "SYNTHETIC_REPLAY_NOT_MARKET_EVIDENCE"
                if self.config.data_mode == "synthetic_fixture"
                else (
                    "CALLER_DECLARED_HISTORICAL_REPLAY_WITH_RETROSPECTIVE_"
                    "NEWS_COVERAGE_NOT_LIVE_READY"
                    if self.config.coverage_mode == "retrospective_audit"
                    else "CALLER_DECLARED_SINGLE_SESSION_PIT_REPLAY_NOT_"
                    "VENDOR_VALIDATED_OR_LIVE_READY"
                )
            ),
            "experiment_id": self.config.experiment_id,
            "start_session": self.config.start_session.isoformat(),
            "end_session": self.config.end_session.isoformat(),
            "sessions": periods,
            "initial_equity": self.config.initial_cash,
            "final_equity": self.ledger.equity,
            "total_return": total_return,
            "cagr_252": cagr,
            "annualized_volatility_252": volatility,
            "sharpe_zero_cash_rate": sharpe,
            "sortino_zero_target": sortino,
            "maximum_intraday_mark_drawdown": self.ledger.maximum_drawdown,
            "maximum_close_to_close_drawdown": max(close_drawdowns, default=0.0),
            "calmar_using_intraday_mark_mdd": (
                cagr / self.ledger.maximum_drawdown
                if cagr is not None and self.ledger.maximum_drawdown > 0 else None
            ),
            "entry_orders": len(self.ledger.orders),
            "entry_fills": sum(fill["side"] == "buy" for fill in self.ledger.fills),
            "exit_fills": sum(fill["side"] == "sell" for fill in self.ledger.fills),
            "trades": len(trades),
            "closed_trades": len(closed),
            "open_trades": len(trades) - len(closed),
            "closed_trade_win_rate": (
                sum(value > 0 for value in closed_net) / len(closed_net) if closed_net else None
            ),
            "closed_trade_average_net_pnl": (
                statistics.mean(closed_net) if closed_net else None
            ),
            "closed_trade_median_net_pnl": (
                statistics.median(closed_net) if closed_net else None
            ),
            "closed_trade_profit_factor": (
                profits / losses if losses > 0 else None
            ),
            "closed_trade_profit_factor_is_infinite": profits > 0 and losses == 0,
            "commission_cash_expense": self.ledger.fees_paid,
            "extra_slippage_embedded_in_fill_prices": self.ledger.slippage_paid,
            "realized_fill_pnl_before_commission": self.ledger.realized_gross_pnl,
            "unrealized_fill_basis_pnl": self.ledger.unrealized_pnl,
            "portfolio_net_pnl_after_commission": portfolio_net_pnl,
            "gross_traded_notional_over_average_equity": turnover,
            "security_session_funnel": {
                "universe_session_rows": len(self.candidates),
                "selected_event_route_scenarios": routes.get("event", 0),
                "next_session_macd_setups": sum(
                    item["macd_setup_signal_session"] is not None
                    for item in self.candidates
                ),
                "price_triggered_signals": signals,
                "submitted_entry_orders": submitted_orders,
                "filled_entries": filled_entries,
                "routes": dict(sorted(routes.items())),
                "terminal_status": dict(sorted(terminal_counts.items())),
                "terminal_reason": dict(sorted(terminal_reasons.items())),
            },
            "bookkeeping_net_pnl_by_executed_branch_variant": dict(
                sorted(branch_pnl.items())
            ),
            "pnl_reconciliation": {
                "portfolio_net_pnl": portfolio_net_pnl,
                "trade_bookkeeping_net_pnl": trade_bookkeeping_net_pnl,
                "trade_to_portfolio_residual": (
                    trade_bookkeeping_net_pnl - portfolio_net_pnl
                ),
                "daily_bookkeeping_net_pnl": daily_bookkeeping_net_pnl,
                "daily_to_portfolio_residual": (
                    daily_bookkeeping_net_pnl - portfolio_net_pnl
                ),
            },
            "cost_semantics": {
                "commission_accounting": "separate_cash_deduction",
                "extra_slippage_accounting": "embedded_in_fill_prices",
                "spread_accounting": "embedded_in_fill_prices",
                "policy_reference_haircut_accounting": "embedded_in_exit_fill_prices",
                "warning": "Do not subtract embedded cost diagnostics from net P&L again.",
            },
            "warnings": warnings,
        }
