"""
FICHIER :
tests/unit/api/test_main.py

RÔLE GÉNÉRAL :
Teste l'application FastAPI et la coordination
entre les routes HTTP, JobStore et RabbitMQBroker.

Aucun véritable broker RabbitMQ n'est démarré.

Les tests remplacent le broker global de main.py
par un faux broker contrôlable.

CIRCULATION TESTÉE :

Django simulé
→ route FastAPI
→ JobStore
→ faux RabbitMQBroker
→ réponse HTTP

ROUTES TESTÉES :

- GET  /health
- POST /v1/jobs
- GET  /v1/jobs/{job_id}

ÉLÉMENTS TESTÉS :

- état de santé de l'API ;
- ouverture et fermeture du lifespan ;
- construction du message worker ;
- création d'un ticket queued ;
- publication dans RabbitMQ ;
- validation HTTP 422 ;
- erreur RabbitMQ HTTP 503 ;
- disparition anormale d'un ticket HTTP 500 ;
- lecture d'un ticket queued ;
- lecture d'un ticket completed ;
- ticket inconnu HTTP 404 ;
- UUID HTTP invalide.

AUCUN SERVICE RÉEL N'EST UTILISÉ :

- aucun conteneur RabbitMQ ;
- aucun worker ;
- aucun serveur Ollama ;
- aucun serveur Uvicorn.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- STARTUP
- CHAT_JOB_CREATE
- CHAT_JOB_PUBLISH
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from app import main
from app.job_store import JobStore
from app.schemas import (
    CreateJobRequest,
    JobState,
    WorkerEventMessage,
    WorkerJobMessage,
)


# Tous les tests utilisent asyncio.
pytestmark = pytest.mark.anyio


# Identifiant stable utilisé afin de rendre
# les résultats des tests prévisibles.
JOB_ID = UUID(
    "12345678-1234-5678-1234-567812345678"
)


# RÔLE :
# Simule RabbitMQBroker.
#
# Cette classe peut :
#
# - indiquer si RabbitMQ est connecté ;
# - enregistrer les tickets publiés ;
# - lever une erreur pendant une publication ;
# - compter les ouvertures et fermetures.
class RecordingBroker:
    def __init__(
        self,
        *,
        connected: bool = True,
        publish_error: Exception | None = None,
    ) -> None:
        self.connected = connected
        self.publish_error = publish_error

        self.published_jobs: list[
            WorkerJobMessage
        ] = []

        self.connect_count = 0
        self.close_count = 0

    # RÔLE :
    # Simule l'ouverture de RabbitMQ
    # pendant le lifespan FastAPI.
    async def connect(self) -> None:
        self.connect_count += 1
        self.connected = True

    # RÔLE :
    # Simule la fermeture de RabbitMQ.
    async def close(self) -> None:
        self.close_count += 1
        self.connected = False

    # RÔLE :
    # Fournit l'état utilisé par GET /health.
    def is_connected(self) -> bool:
        return self.connected

    # RÔLE :
    # Simule la publication d'un ticket.
    #
    # ERREUR :
    # - lève publish_error lorsqu'elle existe.
    async def publish_job(
        self,
        job: WorkerJobMessage,
    ) -> None:
        if self.publish_error is not None:
            raise self.publish_error

        self.published_jobs.append(
            job.model_copy(deep=True)
        )


# RÔLE :
# Simule un stockage dans lequel le ticket
# disparaît immédiatement après sa création.
#
# Ce comportement permet de tester la branche
# HTTP 500 de create_job().
class DisappearingJobStore:
    def __init__(self) -> None:
        self.created_job_ids: list[UUID] = []

        self.failed_publications: list[
            tuple[UUID, str]
        ] = []

    # RÔLE :
    # Enregistre uniquement l'identifiant créé.
    async def create_job(
        self,
        job_id: UUID,
    ) -> None:
        self.created_job_ids.append(job_id)

    # RÔLE :
    # Simule un ticket devenu introuvable.
    async def get_job(
        self,
        job_id: UUID,
    ) -> None:
        return None

    # RÔLE :
    # Enregistre un éventuel échec de publication.
    async def mark_publish_failed(
        self,
        job_id: UUID,
        error: str,
    ) -> None:
        self.failed_publications.append(
            (
                job_id,
                error,
            )
        )


# RÔLE :
# Force pytest-anyio à utiliser asyncio.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# RÔLE :
# Construit un client HTTP asynchrone directement
# relié à l'application FastAPI.
#
# IMPORTANT :
# ASGITransport ne démarre pas Uvicorn
# et n'ouvre aucune connexion réseau.
def _build_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=main.app
    )

    return httpx.AsyncClient(
        transport=transport,
        base_url="http://mycoder.test",
    )


# RÔLE :
# Construit le corps JSON valide
# d'une demande de génération.
def _valid_job_payload() -> dict[str, Any]:
    return {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Explique le fonctionnement "
                    "d'une frégate."
                ),
            }
        ],
    }


# RÔLE :
# Remplace les dépendances globales
# utilisées par les routes FastAPI.
def _install_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    broker: RecordingBroker,
    job_store: object,
) -> None:
    monkeypatch.setattr(
        main,
        "broker",
        broker,
    )

    monkeypatch.setattr(
        main,
        "job_store",
        job_store,
    )


# ---------------------------------------------------------------------------
# Route de santé
# ---------------------------------------------------------------------------


# Chaque état de connexion devient
# un test indépendant.
@pytest.mark.parametrize(
    "connected",
    [
        True,
        False,
    ],
)
async def test_health_reports_rabbitmq_state(
    monkeypatch: pytest.MonkeyPatch,
    connected: bool,
) -> None:
    fake_broker = RecordingBroker(
        connected=connected
    )

    monkeypatch.setattr(
        main,
        "broker",
        fake_broker,
    )

    async with _build_client() as client:
        response = await client.get(
            "/health"
        )

    assert response.status_code == 200

    assert response.json() == {
        "status": "ok",
        "service": "mycoder-api",
        "rabbitmq_connected": connected,
    }


# ---------------------------------------------------------------------------
# Lifespan FastAPI
# ---------------------------------------------------------------------------


# Vérifie que FastAPI ouvre RabbitMQ
# au démarrage puis le ferme à l'arrêt.
async def test_lifespan_connects_and_closes_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker(
        connected=False
    )

    monkeypatch.setattr(
        main,
        "broker",
        fake_broker,
    )

    async with main.lifespan(
        main.app
    ):
        assert fake_broker.connect_count == 1
        assert fake_broker.close_count == 0
        assert fake_broker.connected is True

    assert fake_broker.close_count == 1
    assert fake_broker.connected is False


# ---------------------------------------------------------------------------
# Construction du ticket worker
# ---------------------------------------------------------------------------


# Vérifie que la conversation validée
# devient un WorkerJobMessage.
async def test_build_worker_job_message_preserves_conversation() -> None:
    request = CreateJobRequest(
        messages=[
            {
                "role": "user",
                "content": "Première question.",
            },
            {
                "role": "assistant",
                "content": "Première réponse.",
            },
            {
                "role": "user",
                "content": "Seconde question.",
            },
        ]
    )

    worker_message = (
        main._build_worker_job_message(
            job_id=JOB_ID,
            request=request,
        )
    )

    assert worker_message.job_id == JOB_ID

    assert [
        message.role
        for message in worker_message.messages
    ] == [
        "user",
        "assistant",
        "user",
    ]

    assert (
        worker_message.messages[-1].content
        == "Seconde question."
    )


# ---------------------------------------------------------------------------
# Création normale d'un ticket
# ---------------------------------------------------------------------------


# Vérifie le pipeline principal :
#
# HTTP POST
# → création dans JobStore
# → publication RabbitMQ
# → réponse HTTP 202 queued.
async def test_create_job_route_creates_and_publishes_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker()
    store = JobStore()

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    # Rend l'identifiant prévisible.
    monkeypatch.setattr(
        main,
        "uuid4",
        lambda: JOB_ID,
    )

    async with _build_client() as client:
        response = await client.post(
            "/v1/jobs",
            json=_valid_job_payload(),
        )

    assert response.status_code == 202

    payload = response.json()

    assert payload["job_id"] == str(JOB_ID)
    assert payload["state"] == "queued"
    assert payload["queue_position"] == 1
    assert payload["queue_total"] == 1
    assert payload["progress_percent"] == 0
    assert payload["result"] is None
    assert payload["error"] is None

    assert len(
        fake_broker.published_jobs
    ) == 1

    published_job = (
        fake_broker.published_jobs[0]
    )

    assert published_job.job_id == JOB_ID

    assert (
        published_job.messages[-1].role
        == "user"
    )

    assert (
        published_job.messages[-1].content
        == (
            "Explique le fonctionnement "
            "d'une frégate."
        )
    )

    stored_status = await store.get_job(
        JOB_ID
    )

    assert stored_status is not None
    assert stored_status.state == JobState.QUEUED


# ---------------------------------------------------------------------------
# Validation HTTP 422
# ---------------------------------------------------------------------------


# Chaque corps invalide devient
# un test FastAPI indépendant.
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "messages": [],
            },
            id="conversation-vide",
        ),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": (
                            "Réponse sans question."
                        ),
                    }
                ],
            },
            id="termine-par-assistant",
        ),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Prompt système injecté."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "Bonjour.",
                    },
                ],
            },
            id="role-system-interdit",
        ),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Bonjour.",
                        "champ_inattendu": True,
                    }
                ],
            },
            id="champ-inattendu",
        ),
    ],
)
async def test_create_job_route_rejects_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    fake_broker = RecordingBroker()
    store = JobStore()

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    async with _build_client() as client:
        response = await client.post(
            "/v1/jobs",
            json=payload,
        )

    assert response.status_code == 422

    assert fake_broker.published_jobs == []

    stored_status = await store.get_job(
        JOB_ID
    )

    assert stored_status is None


# ---------------------------------------------------------------------------
# Échec de publication RabbitMQ
# ---------------------------------------------------------------------------


# Vérifie qu'une panne RabbitMQ :
#
# - retourne HTTP 503 ;
# - marque le ticket comme failed ;
# - ne révèle pas le détail technique.
async def test_create_job_route_returns_503_when_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker(
        publish_error=RuntimeError(
            "Détail technique RabbitMQ."
        )
    )

    store = JobStore()

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    monkeypatch.setattr(
        main,
        "uuid4",
        lambda: JOB_ID,
    )

    async with _build_client() as client:
        response = await client.post(
            "/v1/jobs",
            json=_valid_job_payload(),
        )

    assert response.status_code == 503

    assert response.json() == {
        "detail": (
            "Impossible de publier le job "
            "dans RabbitMQ."
        ),
    }

    stored_status = await store.get_job(
        JOB_ID
    )

    assert stored_status is not None
    assert stored_status.state == JobState.FAILED

    assert stored_status.error == (
        "Impossible de publier le job "
        "dans RabbitMQ."
    )

    assert stored_status.result is None
    assert stored_status.progress_percent is None


# ---------------------------------------------------------------------------
# Disparition anormale du ticket
# ---------------------------------------------------------------------------


# Vérifie la protection HTTP 500 lorsque
# le ticket disparaît après sa publication.
async def test_create_job_route_returns_500_when_job_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker()

    disappearing_store = (
        DisappearingJobStore()
    )

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=disappearing_store,
    )

    monkeypatch.setattr(
        main,
        "uuid4",
        lambda: JOB_ID,
    )

    async with _build_client() as client:
        response = await client.post(
            "/v1/jobs",
            json=_valid_job_payload(),
        )

    assert response.status_code == 500

    assert response.json() == {
        "detail": (
            "Le job créé est introuvable "
            "dans le stockage FastAPI."
        ),
    }

    assert (
        disappearing_store.created_job_ids
        == [JOB_ID]
    )

    assert len(
        fake_broker.published_jobs
    ) == 1


# ---------------------------------------------------------------------------
# Lecture d'un ticket queued
# ---------------------------------------------------------------------------


# Vérifie qu'un ticket en attente
# est exposé par GET /v1/jobs/{job_id}.
async def test_get_job_route_returns_queued_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker()
    store = JobStore()

    await store.create_job(
        JOB_ID
    )

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    async with _build_client() as client:
        response = await client.get(
            f"/v1/jobs/{JOB_ID}"
        )

    assert response.status_code == 200

    payload = response.json()

    assert payload["job_id"] == str(JOB_ID)
    assert payload["state"] == "queued"
    assert payload["queue_position"] == 1
    assert payload["queue_total"] == 1
    assert payload["progress_percent"] == 0


# ---------------------------------------------------------------------------
# Lecture d'un ticket completed
# ---------------------------------------------------------------------------


# Vérifie qu'une réponse finale
# est correctement exposée au front.
async def test_get_job_route_returns_completed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker()
    store = JobStore()

    created_status = await store.create_job(
        JOB_ID
    )

    await store.apply_event(
        WorkerEventMessage(
            job_id=JOB_ID,
            state=JobState.COMPLETED,
            occurred_at=(
                created_status.updated_at
                + timedelta(seconds=1)
            ),
            content=(
                "Réponse finale produite "
                "par Qwen."
            ),
            model="qwen-test-model",
        )
    )

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    async with _build_client() as client:
        response = await client.get(
            f"/v1/jobs/{JOB_ID}"
        )

    assert response.status_code == 200

    payload = response.json()

    assert payload["state"] == "completed"
    assert payload["queue_position"] is None
    assert payload["queue_total"] == 0
    assert payload["progress_percent"] == 100
    assert payload["error"] is None

    assert payload["result"] == {
        "content": (
            "Réponse finale produite "
            "par Qwen."
        ),
        "model": "qwen-test-model",
    }


# ---------------------------------------------------------------------------
# Ticket inconnu
# ---------------------------------------------------------------------------


# Vérifie qu'un UUID valide mais inconnu
# retourne une erreur HTTP 404.
async def test_get_job_route_returns_404_for_unknown_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker()
    store = JobStore()

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    async with _build_client() as client:
        response = await client.get(
            f"/v1/jobs/{JOB_ID}"
        )

    assert response.status_code == 404

    assert response.json() == {
        "detail": "Job introuvable.",
    }


# ---------------------------------------------------------------------------
# Identifiant HTTP invalide
# ---------------------------------------------------------------------------


# Vérifie que FastAPI refuse un identifiant
# ne pouvant pas être converti en UUID.
async def test_get_job_route_rejects_invalid_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_broker = RecordingBroker()
    store = JobStore()

    _install_dependencies(
        monkeypatch,
        broker=fake_broker,
        job_store=store,
    )

    async with _build_client() as client:
        response = await client.get(
            "/v1/jobs/identifiant-invalide"
        )

    assert response.status_code == 422