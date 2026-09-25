"""Laden und Validieren der Konfiguration.

Zwei getrennte Quellen, bewusst nicht vermischt:

* ``config.yaml`` -> fachliche Parameter (Schwellenwerte, Pauschalen, Suchraum).
  Darf im Repo liegen und versioniert werden.
* ``.env``        -> Secrets (API-Keys, OAuth-Token). Niemals im Repo.

Zusaetzlich laesst sich jeder YAML-Wert per Umgebungsvariable ueberschreiben --
praktisch fuer Cronjobs und Container, in denen keine Datei angepasst werden
soll::

    SCOUT__MARGIN__MIN_ROI_PERCENT=30 python scout.py

Einstiegspunkt ist :func:`get_settings`; das Ergebnis wird gecacht.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = BASE_DIR / "config.yaml"


class ConfigError(RuntimeError):
    """Konfiguration fehlt, ist unlesbar oder fachlich unplausibel."""


# ---------------------------------------------------------------------------
# Teil-Schemata
# ---------------------------------------------------------------------------
class MarginConfig(BaseModel):
    min_roi_percent: float = Field(25.0, ge=0.0, le=1000.0)
    min_profit_eur: float = Field(12.0, ge=0.0)


class CostConfig(BaseModel):
    shipping_cost_domestic_eur: float = Field(5.49, ge=0.0)
    packaging_cost_eur: float = Field(1.20, ge=0.0)
    ebay_fee_percent: float = Field(12.0, ge=0.0, le=100.0)
    ebay_fixed_fee_eur: float = Field(0.35, ge=0.0)
    return_buffer_percent: float = Field(4.0, ge=0.0, le=100.0)
    payment_fee_percent: float = Field(0.0, ge=0.0, le=100.0)
    import_duty_percent: float = Field(0.0, ge=0.0, le=100.0)
    inbound_shipping_eur: float = Field(0.0, ge=0.0)

    @model_validator(mode="after")
    def _fees_plausible(self) -> CostConfig:
        total = self.ebay_fee_percent + self.payment_fee_percent + self.return_buffer_percent
        if total >= 100.0:
            raise ValueError(
                f"Prozentuale Abzuege ergeben {total:.1f} % des Verkaufspreises "
                "- damit kann kein Verkauf profitabel sein."
            )
        return self


class TaxConfig(BaseModel):
    vat_registered: bool = False
    vat_rate_percent: float = Field(19.0, ge=0.0, lt=100.0)
    input_vat_deductible: bool = False

    @model_validator(mode="after")
    def _deduction_requires_registration(self) -> TaxConfig:
        if self.input_vat_deductible and not self.vat_registered:
            raise ValueError(
                "input_vat_deductible=true setzt vat_registered=true voraus "
                "(Kleinunternehmer duerfen keine Vorsteuer ziehen)."
            )
        return self


class GeminiConfig(BaseModel):
    model: str = "gemini-2.5-flash"
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(1024, gt=0, le=65536)
    timeout_seconds: float = Field(30.0, gt=0.0)
    max_retries: int = Field(4, ge=0, le=10)
    initial_backoff_seconds: float = Field(2.0, gt=0.0)
    max_backoff_seconds: float = Field(60.0, gt=0.0)
    requests_per_minute: int = Field(60, gt=0)
    min_confidence: float = Field(0.80, ge=0.0, le=1.0)
    thinking_budget: int = Field(0, ge=0)

    @field_validator("model")
    @classmethod
    def _model_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("gemini.model darf nicht leer sein.")
        return v

    @model_validator(mode="after")
    def _backoff_ordered(self) -> GeminiConfig:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds muss >= initial_backoff_seconds sein.")
        return self


class CsvSourceConfig(BaseModel):
    offers_path: str = "data/offers.csv"


class HttpSourceConfig(BaseModel):
    timeout_seconds: float = Field(20.0, gt=0.0)
    max_retries: int = Field(3, ge=0, le=10)
    user_agent: str = "ArbitrageScout/1.0 (+headless)"


class SourcesConfig(BaseModel):
    provider: Literal["csv", "mock"] = "csv"
    csv: CsvSourceConfig = Field(default_factory=CsvSourceConfig)
    http: HttpSourceConfig = Field(default_factory=HttpSourceConfig)


class CategoryConfig(BaseModel):
    name: str
    keywords: list[str] = Field(default_factory=list)
    max_purchase_price_eur: float | None = Field(None, gt=0.0)

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("Kategoriename darf nicht leer sein.")
        return v

    @field_validator("keywords")
    @classmethod
    def _non_empty_keywords(cls, v: list[str]) -> list[str]:
        cleaned = [k.strip() for k in v if k and k.strip()]
        if not cleaned:
            raise ValueError("Jede Kategorie braucht mindestens ein Suchwort.")
        return cleaned


class SearchConfig(BaseModel):
    max_candidates_per_run: int = Field(200, gt=0, le=100_000)
    # Wie streng der kostenlose Vorfilter aussortiert, bevor ein LLM-Aufruf
    # faellig wird. 0.0 = nur sichere Verlustgeschaefte verwerfen (teuer, sicher),
    # 1.0 = mit den vollen Schwellen filtern (guenstig, verliert Grenzfaelle).
    prefilter_margin_factor: float = Field(0.5, ge=0.0, le=1.0)
    categories: list[CategoryConfig] = Field(default_factory=list)

    @field_validator("categories")
    @classmethod
    def _unique_names(cls, v: list[CategoryConfig]) -> list[CategoryConfig]:
        names = [c.name for c in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"Doppelte Kategorienamen: {', '.join(dupes)}")
        return v

    def category(self, name: str) -> CategoryConfig | None:
        target = name.strip().lower()
        return next((c for c in self.categories if c.name == target), None)


class OutputConfig(BaseModel):
    results_dir: str = "data"
    write_csv: bool = True
    write_json: bool = True
    only_recommended: bool = False


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    dir: str = "logs"
    filename: str = "scout.log"
    max_bytes: int = Field(5 * 1024 * 1024, gt=0)
    backup_count: int = Field(5, ge=0, le=100)
    console: bool = True
    json_format: bool = False

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Secrets aus .env / Umgebung
# ---------------------------------------------------------------------------
class Secrets(BaseSettings):
    """Zugangsdaten. Als ``SecretStr`` gehalten, damit sie nicht in Logs oder
    Tracebacks landen."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("SCOUT_ENV_FILE", str(BASE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    gemini_api_key: SecretStr | None = None

    ebay_app_id: SecretStr | None = None
    ebay_cert_id: SecretStr | None = None
    ebay_dev_id: SecretStr | None = None
    ebay_oauth_token: SecretStr | None = None

    amazon_paapi_access_key: SecretStr | None = None
    amazon_paapi_secret_key: SecretStr | None = None
    amazon_paapi_partner_tag: str | None = None

    scraper_api_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key and self.gemini_api_key.get_secret_value().strip())

    def require_gemini_key(self) -> str:
        """Key im Klartext -- nur unmittelbar vor dem SDK-Aufruf verwenden."""
        if not self.has_gemini_key:
            raise ConfigError(
                "GEMINI_API_KEY fehlt. Trage ihn in .env ein "
                "(Vorlage: .env.example) oder setze die Umgebungsvariable."
            )
        assert self.gemini_api_key is not None  # durch has_gemini_key garantiert
        return self.gemini_api_key.get_secret_value().strip()


# ---------------------------------------------------------------------------
# Gesamtkonfiguration
# ---------------------------------------------------------------------------
class YamlConfigSource(PydanticBaseSettingsSource):
    """Liest ``config.yaml`` als eigene Settings-Quelle.

    Wichtig ist die Prioritaet: Wuerde die YAML als Init-Argument uebergeben,
    haette sie Vorrang vor den Umgebungsvariablen -- ``SCOUT__...``-Overrides
    waeren wirkungslos. Als eigene Quelle steht sie hinter Env und dotenv.
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


# Von load_settings() gesetzt, bevor AppConfig instanziiert wird.
_yaml_data: dict[str, Any] = {}


class AppConfig(BaseSettings):
    """YAML-Konfiguration mit optionalem Env-Override (``SCOUT__ABSCHNITT__FELD``).

    Prioritaet, absteigend: Init-Argumente > Umgebungsvariablen > .env > YAML.
    """

    model_config = SettingsConfigDict(
        env_prefix="SCOUT__",
        env_nested_delimiter="__",
        extra="forbid",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls, _yaml_data),
            file_secret_settings,
        )

    margin: MarginConfig = Field(default_factory=MarginConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    tax: TaxConfig = Field(default_factory=TaxConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class Settings(BaseModel):
    """Alles, was die Module brauchen -- als ein Objekt, das durchgereicht wird."""

    config: AppConfig
    secrets: Secrets
    config_path: Path
    base_dir: Path = BASE_DIR

    def resolve(self, relative: str | Path) -> Path:
        """Relative Pfade gegen das Projektverzeichnis aufloesen.

        Notwendig fuer Cronjobs, die mit beliebigem Arbeitsverzeichnis starten.
        """
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def log_file(self) -> Path:
        return self.resolve(self.config.logging.dir) / self.config.logging.filename

    @property
    def results_dir(self) -> Path:
        return self.resolve(self.config.output.results_dir)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path} konnte nicht gelesen werden: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} muss auf oberster Ebene ein Mapping enthalten.")
    return raw


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Konfiguration frisch von der Platte lesen und validieren."""
    path = Path(config_path or os.getenv("SCOUT_CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    if not path.is_absolute():
        path = BASE_DIR / path

    global _yaml_data
    _yaml_data = _read_yaml(path)
    try:
        config = AppConfig()
        secrets = Secrets()
    except ConfigError:
        raise
    except Exception as exc:  # ValidationError und Verwandte
        raise ConfigError(f"Ungueltige Konfiguration ({path}):\n{exc}") from exc

    return Settings(config=config, secrets=secrets, config_path=path)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Gecachter Zugriff -- pro Prozess wird genau einmal geladen."""
    return load_settings()


def reset_settings_cache() -> None:
    """Cache leeren (Tests, Reload nach Konfigaenderung)."""
    get_settings.cache_clear()


if __name__ == "__main__":  # Smoke-Test: `python config.py`
    import json

    s = load_settings()
    print(f"Geladen: {s.config_path}")
    print(json.dumps(s.config.model_dump(), indent=2, ensure_ascii=False))
    print("GEMINI_API_KEY gesetzt:", s.secrets.has_gemini_key)
