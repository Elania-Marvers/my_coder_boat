"""
FICHIER :
tests/unit/api/test_job_store.py

RÔLE GÉNÉRAL :
Teste le stockage temporaire des états de jobs
utilisé par l'API FastAPI.

Le JobStore fonctionne entièrement en mémoire.

Ces tests ne démarrent pas :

- FastAPI ;
- RabbitMQ ;
- le worker ;
- Ollama ;
- Django.

ÉLÉMENTS TESTÉS :

- création d'un ticket queued ;
- lecture d'un ticket ;
- calcul des positions dans la file ;
- passage vers running ;
- passage vers completed ;
- passage vers failed ;
- échec de publication RabbitMQ ;
- événements anciens ;
- transitions interdites après un état terminal ;
- restauration d'un ticket depuis un événement RabbitMQ.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_JOB_CREATE
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from datetime import timedelta
from uuid import UUID

import pytest

from app.job_store import JobStore
from app.schemas import (
    JobState,
    JobStatusResponse,
    WorkerEventMessage,
)


# Tous les tests de ce fichier sont asynchrones.
#
# pytest-anyio exécute donc chaque fonction
# dans une boucle asyncio contrôlée.
pytestmark = pytest.mark.anyio


# Identifiants fixes utilisés par les tests.
#
# Des UUID stables rendent les éventuels messages
# d'erreur plus simples à lire.
FIRST_JOB_ID = UUID(
    "11111111-1111-1111-1111-111111111111"
)

SECOND_JOB_ID = UUID(
    "22222222-2222-2222-2222-222222222222"
)

THIRD_JOB_ID = UUID(
    "33333333-3333-3333-3333-333333333333"
)

UNKNOWN_JOB_ID = UUID(
    "99999999-9999-9999-9999-999999999999"
)


# RÔLE :
# Force pytest-anyio à utiliser uniquement asyncio.
#
# RAISON :
# Le JobStore repose sur asyncio.Lock.
#
# Sans cette fixture, pytest-anyio pourrait également
# tenter d'exécuter les tests avec un autre backend.
#
# RETOURNE :
# - "asyncio"
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# RÔLE :
# Construit un JobStore vide pour chaque test.
#
# APPELÉE PAR :
# - pytest avant chaque fonction demandant store.
#
# RETOURNE :
# - Une nouvelle instance indépendante.
@pytest.fixture
def store() -> JobStore:
    return JobStore()


# RÔLE :
# Construit un événement RabbitMQ valide.
#
# APPELÉE PAR :
# - les tests de transition de JobStore.
#
# REÇOIT :
# - le ticket concerné ;
# - le nouvel état ;
# - la date de l'événement ;
# - les champs facultatifs associés à l'état.
#
# RETOURNE :
# - WorkerEventMessage validé par Pydantic.
def _build_event(
    *,
    job_id: UUID,
    state: JobState,
    occurred_at,
    progress_percent: int | None = None,
    content: str | None = None,
    model: str | None = None,
    error: str | None = None,
) -> WorkerEventMessage:
    return WorkerEventMessage(
        job_id=job_id,
        state=state,
        occurred_at=occurred_at,
        progress_percent=progress_percent,
        content=content,
        model=model,
        error=error,
    )


# RÔLE :
# Construit un événement postérieur
# à l'état public actuel d'un job.
#
# APPELÉE PAR :
# - les tests de transitions normales.
#
# RETOURNE :
# - Une date située après status.updated_at.
def _after(
    status: JobStatusResponse,
    *,
    seconds: int = 1,
):
    return (
        status.updated_at
        + timedelta(seconds=seconds)
    )


# ---------------------------------------------------------------------------
# Création et lecture des tickets
# ---------------------------------------------------------------------------


# Vérifie qu'un nouveau ticket est enregistré
# avec l'état queued et une progression de 0 %.
async def test_create_job_returns_queued_status(
    store: JobStore,
) -> None:
    status = await store.create_job(
        FIRST_JOB_ID
    )

    assert status.job_id == FIRST_JOB_ID
    assert status.state == JobState.QUEUED

    assert status.queue_position == 1
    assert status.queue_total == 1

    assert status.progress_percent == 0
    assert status.result is None
    assert status.error is None

    assert status.created_at == status.updated_at


# Vérifie que deux tickets ne peuvent pas
# partager exactement le même UUID.
async def test_create_job_rejects_duplicate_identifier(
    store: JobStore,
) -> None:
    await store.create_job(
        FIRST_JOB_ID
    )

    with pytest.raises(
        ValueError,
        match="existe déjà",
    ):
        await store.create_job(
            FIRST_JOB_ID
        )


# Vérifie que la lecture d'un UUID inconnu
# retourne None plutôt qu'une exception.
async def test_get_job_returns_none_for_unknown_identifier(
    store: JobStore,
) -> None:
    status = await store.get_job(
        UNKNOWN_JOB_ID
    )

    assert status is None


# Vérifie qu'un ticket créé peut être relu
# avec les mêmes informations publiques.
async def test_get_job_returns_created_job(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    stored_status = await store.get_job(
        FIRST_JOB_ID
    )

    assert stored_status is not None

    assert stored_status.job_id == (
        created_status.job_id
    )

    assert stored_status.state == (
        JobState.QUEUED
    )

    assert stored_status.created_at == (
        created_status.created_at
    )


# ---------------------------------------------------------------------------
# Positions dans la file d'attente
# ---------------------------------------------------------------------------


# Vérifie que les tickets queued sont positionnés
# selon leur ordre de création.
async def test_queue_positions_follow_creation_order(
    store: JobStore,
) -> None:
    await store.create_job(
        FIRST_JOB_ID
    )

    await store.create_job(
        SECOND_JOB_ID
    )

    await store.create_job(
        THIRD_JOB_ID
    )

    first_status = await store.get_job(
        FIRST_JOB_ID
    )

    second_status = await store.get_job(
        SECOND_JOB_ID
    )

    third_status = await store.get_job(
        THIRD_JOB_ID
    )

    assert first_status is not None
    assert second_status is not None
    assert third_status is not None

    assert first_status.queue_position == 1
    assert second_status.queue_position == 2
    assert third_status.queue_position == 3

    assert first_status.queue_total == 3
    assert second_status.queue_total == 3
    assert third_status.queue_total == 3


# Vérifie que le ticket suivant remonte
# lorsque le premier passe dans l'état running.
async def test_next_job_moves_forward_when_first_job_starts(
    store: JobStore,
) -> None:
    first_status = await store.create_job(
        FIRST_JOB_ID
    )

    await store.create_job(
        SECOND_JOB_ID
    )

    running_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.RUNNING,
        occurred_at=_after(first_status),
    )

    await store.apply_event(
        running_event
    )

    updated_first_status = await store.get_job(
        FIRST_JOB_ID
    )

    updated_second_status = await store.get_job(
        SECOND_JOB_ID
    )

    assert updated_first_status is not None
    assert updated_second_status is not None

    assert updated_first_status.state == (
        JobState.RUNNING
    )

    assert (
        updated_first_status.queue_position
        is None
    )

    assert updated_first_status.queue_total == 1

    assert updated_second_status.state == (
        JobState.QUEUED
    )

    assert updated_second_status.queue_position == 1
    assert updated_second_status.queue_total == 1


# ---------------------------------------------------------------------------
# Transition vers running
# ---------------------------------------------------------------------------


# Vérifie qu'un événement running retire
# le ticket de la file d'attente.
async def test_running_event_updates_job(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.RUNNING,
        occurred_at=_after(created_status),
    )

    await store.apply_event(event)

    status = await store.get_job(
        FIRST_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.RUNNING
    assert status.queue_position is None
    assert status.queue_total == 0

    # Le worker ne calcule pas encore
    # une progression exacte pendant Ollama.
    assert status.progress_percent is None

    assert status.result is None
    assert status.error is None

    assert status.updated_at == (
        event.occurred_at
    )


# ---------------------------------------------------------------------------
# Transition vers completed
# ---------------------------------------------------------------------------


# Vérifie le pipeline normal :
#
# queued → running → completed
async def test_completed_event_stores_result(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    running_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.RUNNING,
        occurred_at=_after(
            created_status,
            seconds=1,
        ),
    )

    await store.apply_event(
        running_event
    )

    running_status = await store.get_job(
        FIRST_JOB_ID
    )

    assert running_status is not None

    completed_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.COMPLETED,
        occurred_at=_after(
            running_status,
            seconds=1,
        ),
        progress_percent=100,
        content="Réponse finale de Qwen.",
        model="qwen-test-model",
    )

    await store.apply_event(
        completed_event
    )

    completed_status = await store.get_job(
        FIRST_JOB_ID
    )

    assert completed_status is not None

    assert completed_status.state == (
        JobState.COMPLETED
    )

    assert completed_status.queue_position is None
    assert completed_status.queue_total == 0
    assert completed_status.progress_percent == 100
    assert completed_status.error is None

    assert completed_status.result is not None

    assert completed_status.result.content == (
        "Réponse finale de Qwen."
    )

    assert completed_status.result.model == (
        "qwen-test-model"
    )

    assert completed_status.updated_at == (
        completed_event.occurred_at
    )


# ---------------------------------------------------------------------------
# Transition vers failed
# ---------------------------------------------------------------------------


# Vérifie qu'un événement failed
# enregistre proprement le message d'erreur.
async def test_failed_event_stores_error(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    failed_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.FAILED,
        occurred_at=_after(created_status),
        error="Ollama est indisponible.",
    )

    await store.apply_event(
        failed_event
    )

    status = await store.get_job(
        FIRST_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.FAILED
    assert status.queue_position is None
    assert status.queue_total == 0
    assert status.progress_percent is None
    assert status.result is None

    assert status.error == (
        "Ollama est indisponible."
    )


# Vérifie que FastAPI peut marquer le ticket
# comme failed lorsqu'une publication RabbitMQ échoue.
async def test_mark_publish_failed_updates_existing_job(
    store: JobStore,
) -> None:
    await store.create_job(
        FIRST_JOB_ID
    )

    await store.mark_publish_failed(
        job_id=FIRST_JOB_ID,
        error=(
            "Impossible de publier le job "
            "dans RabbitMQ."
        ),
    )

    status = await store.get_job(
        FIRST_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.FAILED

    assert status.error == (
        "Impossible de publier le job "
        "dans RabbitMQ."
    )

    assert status.progress_percent is None
    assert status.result is None
    assert status.queue_position is None


# Vérifie que le signalement d'un échec
# sur un UUID inconnu ne crée pas de ticket.
async def test_mark_publish_failed_ignores_unknown_job(
    store: JobStore,
) -> None:
    await store.mark_publish_failed(
        job_id=UNKNOWN_JOB_ID,
        error="Erreur sans ticket associé.",
    )

    status = await store.get_job(
        UNKNOWN_JOB_ID
    )

    assert status is None


# ---------------------------------------------------------------------------
# Événements anciens et ordre des mises à jour
# ---------------------------------------------------------------------------


# Vérifie qu'un événement plus ancien
# ne remplace pas un état plus récent.
async def test_stale_event_is_ignored(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    running_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.RUNNING,
        occurred_at=_after(
            created_status,
            seconds=10,
        ),
    )

    await store.apply_event(
        running_event
    )

    stale_failed_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.FAILED,
        occurred_at=_after(
            created_status,
            seconds=5,
        ),
        error="Cet événement est trop ancien.",
    )

    await store.apply_event(
        stale_failed_event
    )

    status = await store.get_job(
        FIRST_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.RUNNING
    assert status.error is None

    assert status.updated_at == (
        running_event.occurred_at
    )


# ---------------------------------------------------------------------------
# Protection des états terminaux
# ---------------------------------------------------------------------------


# Vérifie qu'un job completed ne peut pas
# revenir ensuite dans l'état running.
async def test_completed_job_rejects_running_regression(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    completed_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.COMPLETED,
        occurred_at=_after(
            created_status,
            seconds=1,
        ),
        content="Résultat définitif.",
        model="qwen-test-model",
    )

    await store.apply_event(
        completed_event
    )

    running_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.RUNNING,
        occurred_at=(
            completed_event.occurred_at
            + timedelta(seconds=1)
        ),
    )

    await store.apply_event(
        running_event
    )

    status = await store.get_job(
        FIRST_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.COMPLETED
    assert status.result is not None

    assert status.result.content == (
        "Résultat définitif."
    )

    assert status.updated_at == (
        completed_event.occurred_at
    )


# Vérifie qu'un job failed ne peut pas
# devenir completed après son échec définitif.
async def test_failed_job_rejects_completed_regression(
    store: JobStore,
) -> None:
    created_status = await store.create_job(
        FIRST_JOB_ID
    )

    failed_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.FAILED,
        occurred_at=_after(
            created_status,
            seconds=1,
        ),
        error="Échec définitif.",
    )

    await store.apply_event(
        failed_event
    )

    completed_event = _build_event(
        job_id=FIRST_JOB_ID,
        state=JobState.COMPLETED,
        occurred_at=(
            failed_event.occurred_at
            + timedelta(seconds=1)
        ),
        content="Résultat arrivé trop tard.",
        model="qwen-test-model",
    )

    await store.apply_event(
        completed_event
    )

    status = await store.get_job(
        FIRST_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.FAILED
    assert status.error == "Échec définitif."
    assert status.result is None

    assert status.updated_at == (
        failed_event.occurred_at
    )


# ---------------------------------------------------------------------------
# Restauration depuis RabbitMQ
# ---------------------------------------------------------------------------


# Vérifie qu'un événement running reçu après
# un redémarrage de FastAPI recrée le ticket.
async def test_unknown_running_event_recreates_job(
    store: JobStore,
) -> None:
    occurred_at = (
        await store.create_job(
            FIRST_JOB_ID
        )
    ).created_at

    # Utilise ensuite un nouveau stockage vide
    # pour simuler un redémarrage de FastAPI.
    restarted_store = JobStore()

    running_event = _build_event(
        job_id=UNKNOWN_JOB_ID,
        state=JobState.RUNNING,
        occurred_at=(
            occurred_at
            + timedelta(seconds=1)
        ),
    )

    await restarted_store.apply_event(
        running_event
    )

    status = await restarted_store.get_job(
        UNKNOWN_JOB_ID
    )

    assert status is not None

    assert status.job_id == UNKNOWN_JOB_ID
    assert status.state == JobState.RUNNING
    assert status.queue_position is None
    assert status.queue_total == 0
    assert status.progress_percent is None

    assert status.created_at == (
        running_event.occurred_at
    )

    assert status.updated_at == (
        running_event.occurred_at
    )


# Vérifie qu'un résultat completed encore présent
# dans RabbitMQ peut recréer un ticket après redémarrage.
async def test_unknown_completed_event_recreates_job(
    store: JobStore,
) -> None:
    reference_status = await store.create_job(
        FIRST_JOB_ID
    )

    restarted_store = JobStore()

    completed_event = _build_event(
        job_id=UNKNOWN_JOB_ID,
        state=JobState.COMPLETED,
        occurred_at=_after(
            reference_status,
            seconds=1,
        ),
        content="Résultat récupéré après redémarrage.",
        model="qwen-test-model",
    )

    await restarted_store.apply_event(
        completed_event
    )

    status = await restarted_store.get_job(
        UNKNOWN_JOB_ID
    )

    assert status is not None

    assert status.state == JobState.COMPLETED
    assert status.progress_percent == 100
    assert status.error is None
    assert status.result is not None

    assert status.result.content == (
        "Résultat récupéré après redémarrage."
    )

    assert status.result.model == (
        "qwen-test-model"
    )

    assert status.created_at == (
        completed_event.occurred_at
    )