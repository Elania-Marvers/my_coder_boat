"""
FICHIER :
tests/unit/worker/test_config.py

RÔLE GÉNÉRAL :
Teste la configuration Pydantic du worker Qwen.

Les tests couvrent :

- le modèle Ollama ;
- la taille du contexte ;
- la température ;
- l'adresse Ollama ;
- le timeout de génération ;
- le maintien du modèle en mémoire ;
- l'adresse RabbitMQ ;
- les deux files RabbitMQ ;
- les variables d'environnement ;
- le cache de get_settings().

Aucun serveur Ollama ou RabbitMQ
n'est contacté pendant ces tests.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- STARTUP
- CHAT_JOB_CONSUME
- CHAT_JOB_GENERATE
- CHAT_JOB_EVENT
"""

import pytest
from pydantic import ValidationError

from local_qwen_worker.config import (
    WorkerSettings,
    get_settings,
)


# Ensemble des variables lues par WorkerSettings.
WORKER_ENVIRONMENT_VARIABLES = (
    "QWEN_MODEL",
    "QWEN_CONTEXT",
    "QWEN_TEMPERATURE",
    "OLLAMA_BASE_URL",
    "OLLAMA_TIMEOUT_SECONDS",
    "OLLAMA_KEEP_ALIVE",
    "RABBITMQ_URL",
    "RABBITMQ_JOB_QUEUE",
    "RABBITMQ_EVENT_QUEUE",
)


# RÔLE :
# Isole chaque test de l'environnement local
# puis vide le cache des paramètres worker.
@pytest.fixture(autouse=True)
def isolate_worker_settings_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    for variable_name in (
        WORKER_ENVIRONMENT_VARIABLES
    ):
        monkeypatch.delenv(
            variable_name,
            raising=False,
        )

    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


# RÔLE :
# Construit une configuration worker valide
# pouvant être spécialisée par un test.
def _build_settings(
    **overrides: object,
) -> WorkerSettings:
    values: dict[str, object] = {
        "qwen_model": "qwen-test-model",
        "qwen_context": 4096,
        "qwen_temperature": 0.2,
        "ollama_base_url": (
            "http://127.0.0.1:11434"
        ),
        "ollama_timeout_seconds": 60,
        "ollama_keep_alive": "5m",
        "rabbitmq_url": (
            "amqp://guest:guest@127.0.0.1:5672/"
        ),
        "rabbitmq_job_queue": "test.jobs",
        "rabbitmq_event_queue": "test.events",
    }

    values.update(overrides)

    return WorkerSettings(
        **values,
        _env_file=None,
    )


# ---------------------------------------------------------------------------
# Valeurs par défaut
# ---------------------------------------------------------------------------


# Vérifie les valeurs de développement
# intégrées directement dans WorkerSettings.
def test_worker_settings_defaults() -> None:
    settings = WorkerSettings(
        _env_file=None
    )

    assert settings.qwen_model == (
        "qwen2.5-coder:7b-instruct-q4_K_M"
    )

    assert settings.qwen_context == 8192
    assert settings.qwen_temperature == 0.25

    assert settings.ollama_base_url == (
        "http://127.0.0.1:11434"
    )

    assert settings.ollama_timeout_seconds == 300
    assert settings.ollama_keep_alive == "10m"

    assert settings.rabbitmq_url == (
        "amqp://guest:guest@127.0.0.1:5672/"
    )

    assert settings.rabbitmq_job_queue == (
        "mycoder.jobs"
    )

    assert settings.rabbitmq_event_queue == (
        "mycoder.events"
    )


# ---------------------------------------------------------------------------
# Nettoyage et normalisation
# ---------------------------------------------------------------------------


# Vérifie le nettoyage des chaînes
# et la suppression du slash final d'Ollama.
def test_worker_settings_normalise_text_values() -> None:
    settings = _build_settings(
        qwen_model="  qwen-custom  ",
        ollama_base_url=(
            "  http://127.0.0.1:11434///  "
        ),
        ollama_keep_alive="  20m  ",
        rabbitmq_url=(
            "  amqp://guest:guest@localhost:5672/  "
        ),
        rabbitmq_job_queue="  custom.jobs  ",
        rabbitmq_event_queue="  custom.events  ",
    )

    assert settings.qwen_model == "qwen-custom"

    assert settings.ollama_base_url == (
        "http://127.0.0.1:11434"
    )

    assert settings.ollama_keep_alive == "20m"

    assert settings.rabbitmq_url == (
        "amqp://guest:guest@localhost:5672/"
    )

    assert settings.rabbitmq_job_queue == (
        "custom.jobs"
    )

    assert settings.rabbitmq_event_queue == (
        "custom.events"
    )


# ---------------------------------------------------------------------------
# Protocoles
# ---------------------------------------------------------------------------


# Vérifie que HTTPS et AMQPS
# sont tous les deux acceptés.
def test_worker_settings_accept_secure_protocols() -> None:
    settings = _build_settings(
        ollama_base_url=(
            "https://ollama.test/"
        ),
        rabbitmq_url=(
            "amqps://user:password@rabbitmq.test/"
        ),
    )

    assert settings.ollama_base_url == (
        "https://ollama.test"
    )

    assert settings.rabbitmq_url.startswith(
        "amqps://"
    )


# Vérifie qu'une URL Ollama sans protocole HTTP
# est refusée.
def test_worker_settings_reject_invalid_ollama_url() -> None:
    with pytest.raises(
        ValidationError,
        match="http:// ou https://",
    ):
        _build_settings(
            ollama_base_url=(
                "127.0.0.1:11434"
            )
        )


# Vérifie qu'une URL RabbitMQ
# doit utiliser AMQP ou AMQPS.
def test_worker_settings_reject_invalid_rabbitmq_url() -> None:
    with pytest.raises(
        ValidationError,
        match="amqp:// ou amqps://",
    ):
        _build_settings(
            rabbitmq_url=(
                "http://rabbitmq.test"
            )
        )


# ---------------------------------------------------------------------------
# Noms des files
# ---------------------------------------------------------------------------


# Vérifie l'interdiction des espaces
# dans les deux noms de files.
@pytest.mark.parametrize(
    "field_name",
    [
        "rabbitmq_job_queue",
        "rabbitmq_event_queue",
    ],
)
def test_worker_settings_reject_queue_whitespace(
    field_name: str,
) -> None:
    with pytest.raises(
        ValidationError,
        match="ne peut pas contenir d'espace",
    ):
        _build_settings(
            **{
                field_name: "file invalide",
            }
        )


# Vérifie que le préfixe amq.
# reste réservé à RabbitMQ.
@pytest.mark.parametrize(
    "field_name",
    [
        "rabbitmq_job_queue",
        "rabbitmq_event_queue",
    ],
)
def test_worker_settings_reject_reserved_queue_prefix(
    field_name: str,
) -> None:
    with pytest.raises(
        ValidationError,
        match="préfixe `amq.` est réservé",
    ):
        _build_settings(
            **{
                field_name: "amq.interne",
            }
        )


# Vérifie que le worker n'utilise pas
# la même file pour les tickets et les événements.
def test_worker_settings_require_distinct_queues() -> None:
    with pytest.raises(
        ValidationError,
        match="deux files différentes",
    ):
        _build_settings(
            rabbitmq_job_queue="same.queue",
            rabbitmq_event_queue="same.queue",
        )


# ---------------------------------------------------------------------------
# Limites numériques
# ---------------------------------------------------------------------------


# Le contexte doit rester compris
# entre 512 et 131 072 jetons.
@pytest.mark.parametrize(
    "invalid_context",
    [
        511,
        131_073,
    ],
)
def test_worker_settings_reject_invalid_context(
    invalid_context: int,
) -> None:
    with pytest.raises(ValidationError):
        _build_settings(
            qwen_context=invalid_context
        )


# La température doit rester
# comprise entre 0 et 2.
@pytest.mark.parametrize(
    "invalid_temperature",
    [
        -0.01,
        2.01,
    ],
)
def test_worker_settings_reject_invalid_temperature(
    invalid_temperature: float,
) -> None:
    with pytest.raises(ValidationError):
        _build_settings(
            qwen_temperature=(
                invalid_temperature
            )
        )


# Le timeout doit être strictement positif
# et limité à une heure.
@pytest.mark.parametrize(
    "invalid_timeout",
    [
        0,
        3_601,
    ],
)
def test_worker_settings_reject_invalid_timeout(
    invalid_timeout: float,
) -> None:
    with pytest.raises(ValidationError):
        _build_settings(
            ollama_timeout_seconds=(
                invalid_timeout
            )
        )


# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------


# Vérifie la conversion automatique
# des chaînes d'environnement en nombres.
def test_worker_settings_read_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "QWEN_MODEL",
        "environment-qwen",
    )

    monkeypatch.setenv(
        "QWEN_CONTEXT",
        "16384",
    )

    monkeypatch.setenv(
        "QWEN_TEMPERATURE",
        "0.35",
    )

    monkeypatch.setenv(
        "OLLAMA_BASE_URL",
        "https://ollama.environment/",
    )

    monkeypatch.setenv(
        "OLLAMA_TIMEOUT_SECONDS",
        "120",
    )

    monkeypatch.setenv(
        "OLLAMA_KEEP_ALIVE",
        "30m",
    )

    monkeypatch.setenv(
        "RABBITMQ_URL",
        "amqps://user:password@rabbitmq.environment/",
    )

    monkeypatch.setenv(
        "RABBITMQ_JOB_QUEUE",
        "environment.jobs",
    )

    monkeypatch.setenv(
        "RABBITMQ_EVENT_QUEUE",
        "environment.events",
    )

    settings = WorkerSettings(
        _env_file=None
    )

    assert settings.qwen_model == (
        "environment-qwen"
    )

    assert settings.qwen_context == 16_384

    assert settings.qwen_temperature == 0.35

    assert settings.ollama_base_url == (
        "https://ollama.environment"
    )

    assert settings.ollama_timeout_seconds == 120

    assert settings.ollama_keep_alive == "30m"

    assert settings.rabbitmq_job_queue == (
        "environment.jobs"
    )

    assert settings.rabbitmq_event_queue == (
        "environment.events"
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


# Vérifie que la configuration worker
# n'est chargée qu'une seule fois par processus.
def test_get_worker_settings_is_cached() -> None:
    first_settings = get_settings()
    second_settings = get_settings()

    assert first_settings is second_settings