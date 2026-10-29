from __future__ import annotations

from pathlib import Path

import pytest

from omi.config import PROJECT_ROOT, ConfigError, load_settings

EXAMPLE = PROJECT_ROOT / "config" / "config.example.yaml"


def test_example_config_loads_with_absolute_paths():
    s = load_settings(EXAMPLE)
    assert s.config_file == EXAMPLE
    assert s.paths.data_dir == (PROJECT_ROOT / "data").resolve()
    assert s.paths.logs_dir.is_absolute()
    assert s.network.rate_limits["www.cboe.com"] == 0.5
    assert s.source("occ_contract_volume").enabled
    assert s.source("cboe_indices").enabled is False  # VIX family switched off
    assert s.source("finra_regsho").enabled is False
    assert s.source("cftc_cot").model_extra["dataset"] == "gpe5-46if"


def test_source_missing_from_config_counts_as_disabled():
    assert load_settings(EXAMPLE).source("bloomberg").enabled is False


def test_env_vars_override_yaml_and_keep_sibling_keys(monkeypatch):
    monkeypatch.setenv("OMI_SOURCES__FINRA_REGSHO__ENABLED", "true")
    monkeypatch.setenv("OMI_NETWORK__CONNECT_TIMEOUT_S", "3")
    monkeypatch.setenv("OMI_SOURCES__OCC_DAILY_VOLUME__ENABLED", "false")
    s = load_settings(EXAMPLE)
    assert s.source("finra_regsho").enabled is True
    assert s.network.connect_timeout_s == 3
    occ = s.source("occ_daily_volume")
    assert occ.enabled is False
    assert occ.model_extra["backfill_start"] == "2024-11-01"  # untouched by the env override
    assert s.network.rate_limits["default_rps"] == 1.0


def test_omi_config_env_var_selects_file(tmp_path, monkeypatch):
    cfg = tmp_path / "alt.yaml"
    cfg.write_text("paths: {data_dir: ./elsewhere}\n", encoding="utf-8")
    monkeypatch.setenv("OMI_CONFIG", str(cfg))
    s = load_settings()
    assert s.config_file == cfg
    assert s.paths.data_dir == (PROJECT_ROOT / "elsewhere").resolve()


def test_typo_in_yaml_is_rejected(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("network: {proxy: http://proxy:8080}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="proxy"):
        load_settings(cfg)


def test_missing_ca_bundle_is_rejected(tmp_path):
    cfg = tmp_path / "ca.yaml"
    cfg.write_text(f"network: {{ca_bundle: {(tmp_path / 'nope.pem').as_posix()}}}\n",
                   encoding="utf-8")
    with pytest.raises(ConfigError, match="ca_bundle"):
        load_settings(cfg)


def test_missing_explicit_file_is_rejected():
    with pytest.raises(ConfigError, match="not found"):
        load_settings(Path("does/not/exist.yaml"))
