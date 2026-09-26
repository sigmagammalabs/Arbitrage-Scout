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


class ProviderConfig(BaseModel):
    """Einstellungen, die jedes LLM-Backend braucht.

    Retry- und Rate-Limit-Werte stehen bewusst pro Provider und nicht global:
    Geminis Kontingente unterscheiden sich deutlich von denen bei Groq, wo der
    kostenlose Tarif schon bei wenigen Anfragen pro Minute bremst.
    """

    model: str
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(1024, gt=0, le=65536)
    timeout_seconds: float = Field(30.0, gt=0.0)
    max_retries: int = Field(4, ge=0, le=10)
    initial_backoff_seconds: float = Field(2.0, gt=0.0)
    max_backoff_seconds: float = Field(60.0, gt=0.0)
    requests_per_minute: int = Field(60, gt=0)

    @field_validator("model")
    @classmethod
    def _model_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("model darf nicht leer sein.")
        return v

    @model_validator(mode="after")
    def _backoff_ordered(self) -> ProviderConfig:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds muss >= initial_backoff_seconds sein.")
        return self


class GeminiConfig(ProviderConfig):
    # Google zieht Modellversionen regelmaessig fuer Neukunden zurueck (zuletzt
    # 2.5-flash: HTTP 404 statt eines Retry-faehigen Fehlers). Aktuellen Stand
    # pruefen: https://ai.google.dev/gemini-api/docs/models
    model: str = "gemini-3.8-flash"
    # 0 schaltet das interne "Nachdenken" ab -- schneller und guenstiger. Der
    # Produktabgleich ist eine Klassifikation, keine Herleitung.
    thinking_budget: int = Field(0, ge=0)


class GroqConfig(ProviderConfig):
    model: str = "llama-3.3-70b-versatile"
    requests_per_minute: int = Field(30, gt=0)
    # Echte Schema-Erzwingung koennen nur einige Groq-Modelle. Standard ist
    # deshalb der breit unterstuetzte JSON-Modus; der Provider faellt bei einer
    # Ablehnung ohnehin automatisch darauf zurueck.
    json_schema_mode: bool = False


class LLMConfig(BaseModel):
    """Welches Backend den Abgleich uebernimmt und ab wann sein Urteil zaehlt."""

    provider: Literal["gemini", "groq"] = "gemini"
    min_confidence: float = Field(0.80, ge=0.0, le=1.0)

    @field_validator("provider", mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v


class CsvSourceConfig(BaseModel):
    offers_path: str = "data/offers.csv"


class HttpSourceConfig(BaseModel):
    timeout_seconds: float = Field(20.0, gt=0.0)
    max_retries: int = Field(3, ge=0, le=10)
    user_agent: str = "ArbitrageScout/1.0 (+headless)"


class ApiSourceConfig(BaseModel):
    """Automatische Angebotssuche (``sources.provider: api``).

    Die Verkaufsseite ist immer eBay (Browse API). Die Einkaufsseite ist
    umschaltbar, weil die Amazon Creators API nur fuer Associates mit
    mindestens 10 qualifizierten Verkaeufen in 30 Tagen freigeschaltet wird --
    ohne diese Freischaltung laeuft die Automatisierung trotzdem, mit einer
    selbst gepflegten Einkaufsliste als Quelle.
    """

    purchase_source: Literal["csv", "amazon"] = "csv"
    purchase_csv_path: str = "data/purchases.csv"

    amazon_country: str = "DE"
    # Die Creators API liefert hoechstens 10 Treffer pro Suche.
    amazon_results_per_keyword: int = Field(10, ge=1, le=10)

    ebay_marketplace: str = "EBAY_DE"
    # Wie viele eBay-Angebote pro Einkaufsangebot als Kandidaten gelten. Jedes
    # davon kostet spaeter einen LLM-Aufruf, sofern es den Vorfilter besteht.
    ebay_results_per_offer: int = Field(5, ge=1, le=50)
    ebay_new_only: bool = True
    # best_match: eBays Relevanz. price: guenstigste zuerst -- zeigt die
    # schaerfste Konkurrenz, liefert aber oefter Zubehoer statt des Produkts.
    ebay_sort: Literal["best_match", "price"] = "best_match"

    @field_validator("amazon_country", mode="before")
    @classmethod
    def _country_upper(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v


class SourcesConfig(BaseModel):
    provider: Literal["csv", "mock", "api"] = "csv"
    csv: CsvSourceConfig = Field(default_factory=CsvSourceConfig)
    api: ApiSourceConfig = Field(default_factory=ApiSourceConfig)
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


class TelegramConfig(BaseModel):
    """Einstellungen fuer Benachrichtigung (``scout.py --notify-telegram``) und
    den optionalen Listener (``listener.py``). Zugangsdaten stehen in
    ``Secrets``, nicht hier."""

    # Long-Poll-Timeout gegen die Telegram getUpdates-API. Hoeher = weniger
    # Requests, aber traegere Reaktion auf /stop. 20s ist Telegrams eigene
    # Empfehlung fuer Long-Polling.
    long_poll_seconds: float = Field(20.0, gt=0.0, le=50.0)
    # Auch senden, wenn 0 Empfehlungen gefunden wurden -- dient als Herzschlag:
    # Stille kann sonst "nichts gefunden" oder "Cron ist kaputt" bedeuten.
    notify_on_empty: bool = True
    max_recommendations_in_message: int = Field(10, gt=0, le=50)


# ---------------------------------------------------------------------------
# Secrets aus .env / Umgebung
# ---------------------------------------------------------------------------
# Provider-Name -> Feldname in Secrets. Haelt die Pruefung an einer Stelle,
# wenn spaeter ein weiteres Backend dazukommt.
PROVIDER_KEY_FIELDS = {"gemini": "gemini_api_key", "groq": "groq_api_key"}


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
    groq_api_key: SecretStr | None = None

    # eBay Browse API, Application Token (Client Credentials). Im eBay
    # Developer Portal heisst das Paar "App ID (Client ID)" / "Cert ID
    # (Client Secret)". Weitere Keys (Dev ID, User Token) braucht die
    # Browse-Suche nicht.
    ebay_app_id: SecretStr | None = None
    ebay_cert_id: SecretStr | None = None

    # Amazon Creators API (Nachfolger der seit 15.05.2026 abgeschalteten
    # PA-API 5.0). Die Credential-Version (z. B. "2.2" oder "3.2") steht im
    # Associates-Portal neben den Zugangsdaten und bestimmt den OAuth-Endpunkt.
    # Alte AMAZON_PAAPI_*-Eintraege in einer bestehenden .env werden ignoriert.
    amazon_creators_credential_id: SecretStr | None = None
    amazon_creators_credential_secret: SecretStr | None = None
    amazon_creators_credential_version: str | None = None
    amazon_partner_tag: str | None = None

    scraper_api_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    def has_key(self, provider: str) -> bool:
        field = PROVIDER_KEY_FIELDS.get(provider.strip().lower())
        if field is None:
            return False
        value: SecretStr | None = getattr(self, field, None)
        return bool(value and value.get_secret_value().strip())

    def require_key(self, provider: str) -> str:
        """Key im Klartext -- nur unmittelbar vor dem SDK-Aufruf verwenden."""
        provider = provider.strip().lower()
        field = PROVIDER_KEY_FIELDS.get(provider)
        if field is None:
            raise ConfigError(f"Unbekannter LLM-Provider: {provider}")
        if not self.has_key(provider):
            raise ConfigError(
                f"{field.upper()} fehlt. Trage ihn in .env ein "
                "(Vorlage: .env.example) oder setze die Umgebungsvariable."
            )
        return getattr(self, field).get_secret_value().strip()

    @property
    def has_gemini_key(self) -> bool:
        return self.has_key("gemini")

    @property
    def has_groq_key(self) -> bool:
        return self.has_key("groq")

    def require_gemini_key(self) -> str:
        return self.require_key("gemini")

    def require_groq_key(self) -> str:
        return self.require_key("groq")

    @property
    def has_telegram(self) -> bool:
        """Beide Werte muessen gesetzt sein -- ein Bot-Token ohne Chat-ID kann
        nirgendwo hinsenden, eine Chat-ID ohne Token authentifiziert sich nicht."""
        token_set = bool(
            self.telegram_bot_token and self.telegram_bot_token.get_secret_value().strip()
        )
        chat_set = bool(self.telegram_chat_id and self.telegram_chat_id.strip())
        return token_set and chat_set

    def require_telegram(self) -> tuple[str, str]:
        """(bot_token, chat_id) im Klartext -- nur unmittelbar vor dem API-Aufruf verwenden."""
        if not self.has_telegram:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID muessen beide gesetzt sein "
                "(.env oder Umgebungsvariable)."
            )
        assert self.telegram_bot_token is not None
        assert self.telegram_chat_id is not None
        return self.telegram_bot_token.get_secret_value().strip(), self.telegram_chat_id.strip()

    @staticmethod
    def _filled(value: SecretStr | str | None) -> bool:
        if value is None:
            return False
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return bool(raw.strip())

    @property
    def has_ebay(self) -> bool:
        return self._filled(self.ebay_app_id) and self._filled(self.ebay_cert_id)

    def require_ebay(self) -> tuple[str, str]:
        """(client_id, client_secret) im Klartext."""
        if not self.has_ebay:
            raise ConfigError(
                "EBAY_APP_ID und EBAY_CERT_ID fehlen (eBay Developer Portal -> "
                "Application Keys -> Production)."
            )
        assert self.ebay_app_id is not None and self.ebay_cert_id is not None
        return (
            self.ebay_app_id.get_secret_value().strip(),
            self.ebay_cert_id.get_secret_value().strip(),
        )

    @property
    def has_amazon(self) -> bool:
        return all(
            self._filled(v)
            for v in (
                self.amazon_creators_credential_id,
                self.amazon_creators_credential_secret,
                self.amazon_creators_credential_version,
                self.amazon_partner_tag,
            )
        )

    def require_amazon(self) -> tuple[str, str, str, str]:
        """(credential_id, credential_secret, credential_version, partner_tag)."""
        if not self.has_amazon:
            raise ConfigError(
                "Amazon Creators API unvollstaendig: AMAZON_CREATORS_CREDENTIAL_ID, "
                "AMAZON_CREATORS_CREDENTIAL_SECRET, AMAZON_CREATORS_CREDENTIAL_VERSION "
                "und AMAZON_PARTNER_TAG muessen gesetzt sein."
            )
        assert self.amazon_creators_credential_id is not None
        assert self.amazon_creators_credential_secret is not None
        assert self.amazon_creators_credential_version is not None
        assert self.amazon_partner_tag is not None
        return (
            self.amazon_creators_credential_id.get_secret_value().strip(),
            self.amazon_creators_credential_secret.get_secret_value().strip(),
            self.amazon_creators_credential_version.strip(),
            self.amazon_partner_tag.strip(),
        )


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
    llm: LLMConfig = Field(default_factory=LLMConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    groq: GroqConfig = Field(default_factory=GroqConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


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
