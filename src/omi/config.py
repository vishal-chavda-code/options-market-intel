"""Settings loaded from a YAML file, with environment-variable overrides.

Which file is used, first match wins:
  1. an explicit path (``omi --config PATH``)
  2. the ``OMI_CONFIG`` env var
  3. ``<project root>/config/config.yaml`` (git-ignored, machine-specific)
  4. ``<project root>/config/config.example.yaml`` (committed defaults; a warning is logged)

Env vars named ``OMI_<SECTION>__<KEY>`` override the file, e.g.
``OMI_NETWORK__CA_BUNDLE=C:/certs/corp-root.pem`` or ``OMI_SOURCES__FINRA_REGSHO__ENABLED=true``.

Relative paths are resolved against the project root, not the current directory, so a
scheduled task (which often starts in C:/Windows/System32) behaves like an interactive shell.
The project root is ``OMI_HOME`` if set, else the repo this package was installed from with
``pip install -e .``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(os.environ.get("OMI_HOME") or Path(__file__).resolve().parents[2])
REFERENCE_DIR = PROJECT_ROOT / "config" / "reference"
PLACEHOLDER_CONTACT = "you@example.com"


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


class _Section(BaseModel):
    # Typos in the YAML should fail loudly rather than be silently ignored.
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Section):
    data_dir: Path = Path("data")
    exports_dir: Path = Path("exports")
    logs_dir: Path = Path("logs")


class NetworkConfig(_Section):
    https_proxy: str | None = None
    http_proxy: str | None = None
    ca_bundle: Path | None = None
    use_os_truststore: bool = False
    user_agent_contact: str = PLACEHOLDER_CONTACT
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    max_attempts: int = Field(default=4, ge=1)
    # Requests per second, per host. "default_rps" applies to any host not listed.
    rate_limits: dict[str, float] = Field(
        default_factory=lambda: {"default_rps": 1.0, "www.cboe.com": 0.5, "cdn.cboe.com": 1.0}
    )


class UniverseConfig(_Section):
    core: list[str] = Field(default_factory=lambda: ["_SPX", "SPY", "QQQ", "IWM", "_VIX"])
    extra: list[str] = Field(default_factory=list)
    holdings_csv: Path | None = None


class SourceConfig(BaseModel):
    """Per-source settings. Only ``enabled`` is common; each source reads its own extra keys."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = True


class WindowsConfig(_Section):
    short: int = 20
    medium: int = 60
    long: int = 252


class RegimeConfig(_Section):
    calm_vix_pct: float = 30
    stressed_vix_pct: float = 90
    inversion_ratio: float = 1.0


class PipelineConfig(_Section):
    daily_catchup_sessions: int = Field(default=5, ge=0)
    commit_every: int = Field(default=20, ge=1)


class FeaturesConfig(_Section):
    # Upper bounds (calendar days to expiry) of each expiry bucket; anything above the last
    # bound goes in a final open-ended bucket.
    expiry_buckets: list[int] = Field(default_factory=lambda: [0, 7, 30, 90])
    windows: WindowsConfig = Field(default_factory=WindowsConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    gex_sign_convention: str = "dealer_long_calls_short_puts"


class ScheduleConfig(_Section):
    daily_et: str = "19:30"
    retry_et: str = "07:30"
    cot_et: str = "Fri 16:00"


class AppConfig(_Section):
    refresh_seconds: int = 300
    intraday_refresh_seconds: int = 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMI_", env_nested_delimiter="__", extra="forbid")

    paths: PathsConfig = Field(default_factory=PathsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    app: AppConfig = Field(default_factory=AppConfig)

    _config_file: Path | None = PrivateAttr(default=None)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # YAML values arrive as init kwargs; env vars take precedence over them.
        return (env_settings, init_settings)

    @property
    def config_file(self) -> Path | None:
        """The YAML file these settings were loaded from."""
        return self._config_file

    def source(self, name: str) -> SourceConfig:
        """Config for one source; a source missing from the file counts as disabled."""
        return self.sources.get(name) or SourceConfig(enabled=False)


def resolve_path(p: Path) -> Path:
    """Expand ``~`` and env vars, then anchor relative paths at the project root."""
    p = Path(os.path.expandvars(str(p))).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def find_config_file(explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = resolve_path(explicit)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    if env := os.environ.get("OMI_CONFIG"):
        path = resolve_path(Path(env))
        if not path.is_file():
            raise ConfigError(f"OMI_CONFIG points to a missing file: {path}")
        return path
    local = PROJECT_ROOT / "config" / "config.yaml"
    if local.is_file():
        return local
    example = PROJECT_ROOT / "config" / "config.example.yaml"
    if example.is_file():
        log.warning("config/config.yaml not found; using config.example.yaml defaults")
        return example
    raise ConfigError(f"No config file found under {PROJECT_ROOT / 'config'}")


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from YAML plus ``OMI_*`` env overrides, with paths made absolute."""
    path = find_config_file(config_path)
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    try:
        settings = Settings(**raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {path}:\n{exc}") from exc

    settings._config_file = path
    settings.paths.data_dir = resolve_path(settings.paths.data_dir)
    settings.paths.exports_dir = resolve_path(settings.paths.exports_dir)
    settings.paths.logs_dir = resolve_path(settings.paths.logs_dir)
    if settings.universe.holdings_csv is not None:
        settings.universe.holdings_csv = resolve_path(settings.universe.holdings_csv)

    net = settings.network
    if net.ca_bundle is not None:
        net.ca_bundle = resolve_path(net.ca_bundle)
        if not net.ca_bundle.is_file():
            raise ConfigError(f"network.ca_bundle points to a missing file: {net.ca_bundle}")
    if net.user_agent_contact == PLACEHOLDER_CONTACT:
        log.warning(
            "network.user_agent_contact is still the placeholder; set it in config/config.yaml"
        )
    return settings
