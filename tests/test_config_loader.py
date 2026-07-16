"""Tests for config.loader — TOML config flattening + CLI precedence."""
import sys

from config.loader import config_defaults_from_argv, load_toml_overrides


def test_load_toml_flattens_sections(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[strategy]\n'
        'leverage = 3\n'
        'symbol = "ETHUSDT"\n'
        '[runner]\n'
        'interval = 9\n'
        'mainnet = true\n'
    )
    d = load_toml_overrides(str(p))
    assert d == {"leverage": 3, "symbol": "ETHUSDT", "interval": 9, "mainnet": True}


def test_load_toml_missing_file_returns_empty(tmp_path):
    assert load_toml_overrides(str(tmp_path / "nope.toml")) == {}


def test_config_defaults_from_argv_picks_config(monkeypatch, tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[strategy]\nleverage = 4\n')
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(p)])
    d = config_defaults_from_argv()
    assert d == {"leverage": 4}


def test_config_defaults_falls_back_to_default_path(monkeypatch, tmp_path):
    # No --config flag and a non-existent default path => empty overrides.
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert config_defaults_from_argv(default_path=str(tmp_path / "nope.toml")) == {}


def test_cli_overrides_toml_via_set_defaults(monkeypatch, tmp_path):
    """End-to-end: argparse set_defaults(TOML) then CLI flag wins."""
    import argparse

    p = tmp_path / "c.toml"
    p.write_text('[runner]\ninterval = 9\n')
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(p), "--interval", "2"])

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--interval", type=int, default=60)
    ap.set_defaults(**config_defaults_from_argv())
    args = ap.parse_args()
    # TOML said 9, but CLI said 2 -> CLI wins.
    assert args.interval == 2


def test_toml_overrides_builtin_default(monkeypatch, tmp_path):
    """Without a CLI flag, the TOML value beats the built-in default."""
    import argparse

    p = tmp_path / "c.toml"
    p.write_text('[runner]\ninterval = 9\n')
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(p)])

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--interval", type=int, default=60)
    ap.set_defaults(**config_defaults_from_argv())
    args = ap.parse_args()
    # No CLI flag -> TOML (9) beats built-in default (60).
    assert args.interval == 9
