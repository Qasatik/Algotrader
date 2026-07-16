"""Async multi-tenant bot orchestrator.

Manages one :class:`CarryStrategy` per active user, running each in the
asyncio thread pool (the strategy/exchange stack is synchronous pybit HTTP).
The supervisor periodically reconciles the tenant set against the database
(new users spawn a task; disabled users are stopped).

Tier enforcement: ``max_notional`` is clamped to the user's subscription tier
so a FREE user can never deploy $5000. Expired subscriptions run in
*monitoring mode* (``can_open=False``) so open positions are still protected
by the basis-guard / close logic, but no NEW positions open until renewal.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from core.carry_strategy import CarryConfig, CarryStrategy
from core.exchange import BybitExchange
from saas.models import BotConfig, User
from saas.user_manager import UserManager

log = structlog.get_logger()

#: Callable(api_key, api_secret) -> BybitExchange.
ExchangeFactory = Callable[[str, str], BybitExchange]


def default_exchange_factory(api_key: str, api_secret: str) -> BybitExchange:
    """Production factory: a real mainnet exchange with the user's keys."""
    return BybitExchange(testnet=False, api_key=api_key, api_secret=api_secret)


@dataclass
class _Tenant:
    """A running per-user bot instance."""

    user_id: int
    strategy: CarryStrategy
    exchange: BybitExchange
    task: asyncio.Task | None = None
    last_poll: float = 0.0
    last_status: str = "init"


class TenantRunner:
    """Supervisor that keeps one bot task alive per active user."""

    def __init__(
        self,
        mgr: UserManager,
        exchange_factory: ExchangeFactory | None = None,
        poll_interval: float = 60.0,
        resync_interval: float = 300.0,
    ) -> None:
        self.mgr = mgr
        self._factory: ExchangeFactory = exchange_factory or default_exchange_factory
        self.poll_interval = poll_interval
        self.resync_interval = resync_interval
        self._tenants: dict[int, _Tenant] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ config

    def build_carry_config(self, user: User, bc: BotConfig) -> CarryConfig:
        """Build a CarryConfig for *user*, clamping notional to their tier."""
        return CarryConfig(
            leverage=bc.leverage,
            equity_fraction=bc.equity_fraction,
            min_funding_to_open=bc.min_funding,
            max_notional=bc.resolved_max_notional(user.effective_tier),
            min_notional=5.0,
            stop_loss_pct=bc.stop_loss_pct,
        )

    # ------------------------------------------------------------- one poll

    def poll_user(self, user_id: int) -> str:
        """Run ONE decide→execute cycle for a user. Returns a status string.

        Synchronous core — testable without asyncio. Steps:
        1. Load user + config from the DB.
        2. Ensure a tenant (exchange + strategy) exists and is reconciled.
        3. Run one cycle with ``can_open`` = (subscribed AND bot_enabled).
        """
        user = self.mgr.get_by_id(user_id)
        if user is None or not user.has_api_key:
            return "no user / no api key"
        bc = self.mgr.get_bot_config(user_id)
        cfg = self.build_carry_config(user, bc)

        tenant = self._tenants.get(user_id)
        if tenant is None:
            creds = self.mgr.get_api_credentials(user_id)
            if creds is None:
                return "no api credentials"
            exchange = self._factory(*creds)
            strategy = CarryStrategy(exchange, cfg)
            strategy.reconcile()
            tenant = _Tenant(user_id=user_id, strategy=strategy, exchange=exchange)
            self._tenants[user_id] = tenant
        else:
            # Refresh config in case the user changed settings / tier.
            tenant.strategy.cfg = cfg

        # Expired subscription → monitoring only (protect open positions,
        # but don't open new ones until renewal).
        can_open = user.is_subscribed
        try:
            act = tenant.strategy.decide(can_open=can_open)
            if act.action not in ("none", "hold"):
                tenant.strategy.execute(act)
            status = f"{act.action}: {act.reason}"
        except Exception as exc:  # noqa: BLE001 — tenant must not crash supervisor
            status = f"error: {exc}"
            log.error("tenant_poll_failed", user_id=user_id, error=str(exc))

        tenant.last_poll = time.time()
        tenant.last_status = status
        log.info("tenant_poll", user_id=user_id, status=status,
                 tier=user.effective_tier.value, can_open=can_open)
        return status

    # ----------------------------------------------------------- supervisor

    async def _run_user_loop(self, user_id: int) -> None:
        """Per-user polling loop (decide/execute runs in the thread pool)."""
        while not self._stop.is_set() and user_id in self._tenants:
            try:
                await asyncio.to_thread(self.poll_user, user_id)
            except Exception as exc:  # noqa: BLE001
                log.error("tenant_loop_error", user_id=user_id, error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass  # poll interval elapsed → next cycle

    def _sync_tenants(self) -> None:
        """Reconcile the tenant set against active users (sync, thread-safe).

        New active users are primed via :meth:`poll_user` (so setup errors
        surface immediately); their async loop is spawned by :meth:`run`.
        Stale tenants (user disabled / keys removed) are dropped.
        """
        active = self.mgr.list_active_bots()
        active_ids = {u.id for u in active}
        for uid in list(self._tenants):
            if uid not in active_ids:
                log.info("tenant_removed", user_id=uid)
                tenant = self._tenants.pop(uid)
                if tenant.task and not tenant.task.done():
                    tenant.task.cancel()
        for u in active:
            if u.id not in self._tenants:
                log.info("tenant_added", user_id=u.id,
                         telegram_id=u.telegram_id, tier=u.effective_tier.value)
                self.poll_user(u.id)  # prime synchronously

    async def run(self) -> None:
        """Main supervisor loop: reconcile tenants periodically + spawn loops."""
        log.info("tenant_runner_start", poll_interval=self.poll_interval,
                 resync_interval=self.resync_interval)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._sync_tenants)
                # Spawn async loops for any newly-primed tenants.
                for tenant in self._tenants.values():
                    if tenant.task is None or tenant.task.done():
                        tenant.task = asyncio.create_task(
                            self._run_user_loop(tenant.user_id))
            except Exception as exc:  # noqa: BLE001
                log.error("tenant_sync_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.resync_interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        """Signal the supervisor and all tenant loops to stop."""
        self._stop.set()
        for tenant in self._tenants.values():
            if tenant.task and not tenant.task.done():
                tenant.task.cancel()

    # ------------------------------------------------------------- introspection

    def tenant_status(self) -> list[dict]:
        """Snapshot of all running tenants (for /status in Telegram)."""
        return [
            {"user_id": t.user_id, "last_status": t.last_status,
             "last_poll_ago_s": round(time.time() - t.last_poll, 0)
             if t.last_poll else None}
            for t in self._tenants.values()
        ]
