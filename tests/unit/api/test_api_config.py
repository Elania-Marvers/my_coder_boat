"""
FICHIER :
tests/unit/api/test_config.py

RÔLE GÉNÉRAL :
Teste la configuration Pydantic utilisée
par l'API FastAPI.

Les tests vérifient :

- les valeurs par défaut ;
- la lecture des variables d'environnement ;
- le nettoyage des chaînes ;
- les protocoles AMQP et AMQPS ;
- la validité des noms de files ;
- la séparation entre jobs et événements ;
- le cache de get_settings().

Aucun service externe n'est démarré.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- STARTUP
- CHAT_JOB_PUBLISH
- CHAT_JOB_EVENT
"""

import pytest
from pydantic import ValidationError

from app.config import (
    ApiSettings,
    get_settings,
)


# Variables appartenant à ApiSettings.
#
# Cette liste permet d'isoler les tests
# du fichier .env chargé par le Makefile.
API_ENVIRONMENT_VARIABLES = (
    "RABBITMQ_URL",
    "RABBITMQ_JOB_QUEUE",
    "RABBITMQ_EVENT_QUEUE",
)


# RÔLE :
# Nettoie l'environnement avant chaque test
# puis vide le cache de get_settings().
#
# RAISON :
# Le Makefile exporte les variables de .env.
# Les tests doivent rester prévisibles même si
# le développeur modifie sa configuration locale.
@pytest.fixture(autouse=True)
def isolate_api_settings_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    for variable_name in (
        API_ENVIRONMENT_VARIABLES
    ):
        monkeypatch.delenv(
            variable_name,
            raising=False,
        )

    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


# RÔLE :
# Construit une configuration valide
# sans lire le fichier .env.
def _build_settings(
    **overrides: object,
) -> ApiSettings:
    values: dict[str, object] = {
        "rabbitmq_url": (
            "amqp://guest:guest@127.0.0.1:5672/"
        ),
        "rabbitmq_job_queue": "test.jobs",
        "rabbitmq_event_queue": "test.events",
    }

    values.update(overrides)

    return ApiSettings(
        **values,
        _env_file=None,
    )


# ---------------------------------------------------------------------------
# Valeurs par défaut
# ---------------------------------------------------------------------------


# Vérifie les valeurs utilisées lorsque
# ni l'environnement ni .env ne les fournissent.
def test_api_settings_defaults() -> None:
    settings = ApiSettings(
        _env_file=None
    )

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
# Nettoyage des chaînes
# ---------------------------------------------------------------------------


# Vérifie que les espaces extérieurs
# sont retirés avant validation.
def test_api_settings_strip_text_values() -> None:
    settings = _build_settings(
        rabbitmq_url=(
            "  amqp://guest:guest@localhost:5672/  "
        ),
        rabbitmq_job_queue="  custom.jobs  ",
        rabbitmq_event_queue="  custom.events  ",
    )

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
# Validation de l'adresse RabbitMQ
# ---------------------------------------------------------------------------


# Vérifie que le protocole sécurisé AMQPS
# est également autorisé.
def test_api_settings_accept_amqps_url() -> None:
    settings = _build_settings(
        rabbitmq_url=(
            "amqps://user:password@rabbitmq.test/"
        )
    )

    assert settings.rabbitmq_url.startswith(
        "amqps://"
    )


# Vérifie qu'une adresse HTTP
# ne peut pas être utilisée comme adresse AMQP.
def test_api_settings_reject_invalid_rabbitmq_url() -> None:
    with pytest.raises(
        ValidationError,
        match="amqp:// ou amqps://",
    ):
        _build_settings(
            rabbitmq_url=(
                "http://127.0.0.1:5672"
            )
        )


# ---------------------------------------------------------------------------
# Validation des noms de files
# ---------------------------------------------------------------------------


# Les deux champs utilisent le même validateur.
#
# Le paramétrage crée donc deux tests pytest.
@pytest.mark.parametrize(
    "field_name",
    [
        "rabbitmq_job_queue",
        "rabbitmq_event_queue",
    ],
)
def test_api_settings_reject_queue_whitespace(
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


# Vérifie que le préfixe interne amq.
# est interdit pour les deux files.
@pytest.mark.parametrize(
    "field_name",
    [
        "rabbitmq_job_queue",
        "rabbitmq_event_queue",
    ],
)
def test_api_settings_reject_reserved_queue_prefix(
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


# Vérifie que jobs et events
# ne peuvent pas désigner la même file.
def test_api_settings_require_distinct_queues() -> None:
    with pytest.raises(
        ValidationError,
        match="deux files différentes",
    ):
        _build_settings(
            rabbitmq_job_queue="same.queue",
            rabbitmq_event_queue="same.queue",
        )


# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------


# Vérifie que BaseSettings lit correctement
# les variables d'environnement en majuscules.
def test_api_settings_read_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RABBITMQ_URL",
        "amqps://user:password@rabbitmq.test/",
    )

    monkeypatch.setenv(
        "RABBITMQ_JOB_QUEUE",
        "environment.jobs",
    )

    monkeypatch.setenv(
        "RABBITMQ_EVENT_QUEUE",
        "environment.events",
    )

    settings = ApiSettings(
        _env_file=None
    )

    assert settings.rabbitmq_url == (
        "amqps://user:password@rabbitmq.test/"
    )

    assert settings.rabbitmq_job_queue == (
        "environment.jobs"
    )

    assert settings.rabbitmq_event_queue == (
        "environment.events"
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


# Vérifie que get_settings() retourne
# la même instance pendant le processus.
def test_get_api_settings_is_cached() -> None:
    first_settings = get_settings()
    second_settings = get_settings()

    assert first_settings is second_settings