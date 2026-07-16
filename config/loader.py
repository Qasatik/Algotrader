"""TOML config loader for the carry bot.

Lets long-running services read their parameters from ``config/carry.toml``
instead of a 15-argument command line.  The loader returns a flat
``{argparse_dest: value}`` dict suitable for ``ArgumentParser.set_defaults()``,
so CLI flags always override the file (file > built-in default, CLI > file).

Sections in the TOML are organisational only — they are flattened, so
``[strategy] leverage = 2`` becomes ``{"leverage": 2}``.  Unknown keys are
ignored.

Requires Python 3.11+ (stdlib :mod:`tomllib`).

Example ``config/carry.toml``::

    [strategy]
    symbol        = "BTCUSDT"
    leverage      = 2
    min_funding_to_open = 0.0001

    [runner]
    interval      = 5
    mainnet       = true
    pnl_log       = "data/carry_pnl.csv"
"""
from __future__ import annotations

import argparse
from pathlib import Path

import tomllib


def load_toml_overrides(path: str) -> dict:
    """Load a TOML config file into a flat ``{dest: value}`` overrides dict.

    All sections are merged into one flat dict.  Returns ``{}`` if the file
    does not exist (so a missing config is a no-op).
    """
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        data = tomllib.load(f)
    flat: dict = {}
    for section in data.values():
        if isinstance(section, dict):
            flat.update(section)
    return flat


def config_defaults_from_argv(default_path: str = "config/carry.toml") -> dict:
    """Pre-scan ``sys.argv`` for ``--config PATH`` and return its overrides.

    If ``--config`` is absent, falls back to *default_path* (if it exists).
    Used to seed ``ArgumentParser.set_defaults()`` before the real parse, so
    every CLI flag still wins over the file.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    known, _ = pre.parse_known_args()
    path = known.config or default_path
    return load_toml_overrides(path)
