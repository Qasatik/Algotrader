"""API-key security audit for the carry bot (P3-13).

Confirms the trading key has the permissions it NEEDS (spot + derivatives
trade) and LACKS the permissions that would be catastrophic if abused
(wallet transfers, withdrawals). Run once at startup so a misconfigured or
over-privileged key is caught before any order is placed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.exchange import BybitExchange
from utils.logger import get_logger

log = get_logger("security")

# Permission groups that let money LEAVE the account or move between accounts.
# ANY entry here on a trading key is a red flag.
_DANGEROUS_GROUPS = {"Wallet"}

# Specific permission strings that are always dangerous regardless of group.
_DANGEROUS_PERMS = {"Withdraw", "AccountTransfer", "SubMemberTransfer"}

# Groups that grant trading capability (at least one must be non-empty for the
# bot to function).
_TRADE_GROUPS = ("ContractTrade", "Spot", "Derivatives", "Options")


@dataclass
class ApiKeyAudit:
    """Result of auditing the live API key's permission scope."""

    ok: bool  # True = safe to proceed (no dangerous/blocking perms)
    note: str  # the key's label/remark
    permissions: dict[str, list[str]]
    ip_whitelist: list[str]
    expires_at: str | None
    read_only: bool
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    error: str | None = None  # set if the audit itself failed (API unreachable)

    @property
    def has_ip_whitelist(self) -> bool:
        """True if the key is locked to specific IPs (not open to the world)."""
        return bool(self.ip_whitelist) and self.ip_whitelist != ["*"]

    @property
    def days_to_expiry(self) -> float | None:
        """Days until the key expires, or None if it doesn't expire / unparseable."""
        if not self.expires_at:
            return None
        try:
            dt = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except (ValueError, TypeError):
            return None


def audit_api_key(exchange: BybitExchange) -> ApiKeyAudit:
    """Audit the live API key: trade perms present, fund-move perms absent.

    Returns an :class:`ApiKeyAudit`. ``ok`` is ``False`` only for blocking
    issues (dangerous wallet permissions, read-only, no trade perms, or an
    already-expired key). A missing IP whitelist is a *warning*, not a blocker.
    """
    try:
        info = exchange.get_api_key_info()
    except Exception as exc:  # noqa: BLE001 — audit must never crash the runner
        log.error("api_key_audit_failed", error=str(exc))
        return ApiKeyAudit(
            ok=False,
            note="",
            permissions={},
            ip_whitelist=[],
            expires_at=None,
            read_only=False,
            error=f"could not query API key info: {exc}",
        )

    perms: dict[str, list[str]] = info.get("permissions", {}) or {}
    # Bybit returns permission values as lists; coerce defensively.
    perms = {k: (v if isinstance(v, list) else []) for k, v in perms.items()}
    ips: list[str] = info.get("ips", []) or []
    note = str(info.get("note", "") or "")
    expires = info.get("expiredAt") or None
    read_only = bool(info.get("readOnly", 0))

    warnings: list[str] = []
    infos: list[str] = []
    ok = True

    # 1) Dangerous permission groups (money movement) — HARD BLOCK.
    for group in _DANGEROUS_GROUPS:
        entries = perms.get(group, [])
        if entries:
            ok = False
            warnings.append(
                f"CRITICAL: key has '{group}' permission {entries} — funds could "
                "be transferred out. Remove this permission before trading."
            )

    # 2) Dangerous individual perms anywhere — HARD BLOCK.
    for group, entries in perms.items():
        for e in entries:
            if e in _DANGEROUS_PERMS:
                ok = False
                warnings.append(
                    f"CRITICAL: permission '{e}' in group '{group}' allows fund "
                    "movement."
                )

    # 3) Read-only key can't trade — HARD BLOCK.
    if read_only:
        ok = False
        warnings.append("key is read-only — cannot place orders.")

    # 4) No trade permission at all — HARD BLOCK.
    if not any(perms.get(g) for g in _TRADE_GROUPS):
        ok = False
        warnings.append("no trade permission found — bot cannot place orders.")

    # 5) IP whitelist (soft warning — not a blocker, but a real risk).
    if not (ips and ips != ["*"]):
        warnings.append(
            "no IP whitelist (ips=['*']) — key works from ANY IP. If it leaks, "
            "anyone can trade your account. Add an IP whitelist in Bybit settings."
        )

    audit = ApiKeyAudit(
        ok=ok,
        note=note,
        permissions=perms,
        ip_whitelist=ips,
        expires_at=expires,
        read_only=read_only,
        warnings=warnings,
        infos=infos,
    )

    # 6) Expiry awareness.
    d = audit.days_to_expiry
    if d is not None:
        if d < 0:
            audit.warnings.append(
                f"key EXPIRED {abs(d):.0f} days ago ({expires}) — renew it."
            )
            audit.ok = False
        elif d < 14:
            audit.warnings.append(
                f"key expires in {d:.0f} days ({expires}) — renew soon."
            )
        else:
            audit.infos.append(f"key expires in {d:.0f} days ({expires}).")

    active = {g: v for g, v in perms.items() if v}
    log.info("api_key_audit", ok=audit.ok, note=note, active_perms=active, ips=ips)
    return audit


def format_audit(audit: ApiKeyAudit) -> str:
    """Human-readable multi-line report for CLI / logs / Telegram."""
    lines = ["API Key Security Audit"]
    lines.append(f"  note:         {audit.note or '(none)'}")
    lines.append(f"  read-only:    {audit.read_only}")
    lines.append(
        f"  IP whitelist: {'yes (' + ', '.join(audit.ip_whitelist) + ')' if audit.has_ip_whitelist else 'NO (open to all IPs)'}"
    )
    if audit.expires_at:
        d = audit.days_to_expiry
        if d is not None:
            lines.append(f"  expires:      {audit.expires_at} ({d:+.0f} days)")
        else:
            lines.append(f"  expires:      {audit.expires_at}")
    active = {g: v for g, v in audit.permissions.items() if v}
    lines.append(f"  permissions:  {active if active else '(none)'}")
    if audit.error:
        lines.append(f"  ERROR: {audit.error}")
    for w in audit.warnings:
        lines.append(f"  ⚠️  {w}")
    for i in audit.infos:
        lines.append(f"  ℹ️  {i}")
    lines.append(f"  verdict: {'✅ SAFE' if audit.ok else '❌ ACTION NEEDED'}")
    return "\n".join(lines)


def startup_audit(exchange: BybitExchange, *, skip: bool = False) -> bool:
    """Run the API-key audit at startup. Returns True if safe to proceed.

    Prints the formatted report to stdout. On a *blocking* failure (dangerous
    wallet permissions, read-only, no trade perms, expired key) returns
    ``False`` so the caller can abort. If the audit *itself* fails (API
    endpoint unreachable) it warns but returns ``True`` — we don't block live
    trading just because the audit endpoint had a transient hiccup.
    """
    if skip:
        log.warning("api_key_audit_skipped")
        print("⚠️ API key audit skipped (--skip-api-audit).")
        return True
    audit = audit_api_key(exchange)
    print("\n" + format_audit(audit))
    if audit.error:
        print(
            "\n⚠️ API key audit could not run (see above). Proceeding — "
            "verify key permissions manually in Bybit settings."
        )
        return True
    if not audit.ok:
        print(
            "\n❌ API key audit FAILED — refusing to trade with an unsafe key.\n"
            "Fix the permissions in Bybit, or pass --skip-api-audit to override."
        )
    else:
        print()
    return audit.ok
