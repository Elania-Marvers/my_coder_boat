"""
FICHIER :
tests/integration/test_chat_pipeline.py

RÔLE GÉNÉRAL :
Teste le pipeline complet de conversation MyCoder
sans démarrer de véritables serveurs externes.

Le test relie réellement :

navigateur Django simulé
→ vue Django
→ client métier chat.services
→ route FastAPI
→ JobStore
→ broker en mémoire
→ RabbitMQWorker
→ faux service Qwen
→ événements running/completed/failed
→ JobStore
→ route FastAPI de statut
→ vue Django
→ session du navigateur

COMPOSANTS RÉELS UTILISÉS :

- vues Django ;
- sessions Django ;
- chat.services.create_job() ;
- chat.services.get_job_status() ;
- routes FastAPI ;
- contrats Pydantic de l'API ;
- JobStore ;
- RabbitMQWorker ;
- contrats Pydantic du worker ;
- finalisation de la conversation Django.

COMPOSANTS REMPLACÉS :

- le transport HTTP réseau ;
- le serveur RabbitMQ ;
- le serveur Ollama ;
- le modèle Qwen.

SCÉNARIOS TESTÉS :

- génération réussie ;
- génération échouée ;
- conversation en plusieurs tours.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_JOB_CREATE
- CHAT_JOB_PUBLISH
- CHAT_JOB_CONSUME
- CHAT_JOB_GENERATE
- CHAT_JOB_EVENT
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from django.test import Client
from django.urls import reverse
from fastapi.testclient import TestClient

from app import main
from app.job_store import JobStore
from app.schemas import (
    WorkerEventMessage as ApiWorkerEventMessage,
)
from app.schemas import (
    WorkerJobMessage as ApiWorkerJobMessage,
)
from chat import services as front_services
from chat.services import ApiClientError
from chat.views import (
    ACTIVE_JOB_SESSION_KEY,
    HISTORY_SESSION_KEY,
    MODEL_SESSION_KEY,
)
from local_qwen_worker.config import WorkerSettings
from local_qwen_worker.ollama_client import (
    OllamaUnavailableError,
)
from local_qwen_worker.prompts import (
    DEFAULT_SYSTEM_PROMPT,
)
from local_qwen_worker.rabbitmq_worker import (
    RabbitMQWorker,
)
from local_qwen_worker.rabbitmq_worker import (
    WorkerEventMessage as RuntimeWorkerEventMessage,
)
from local_qwen_worker.rabbitmq_worker import (
    WorkerJobMessage as RuntimeWorkerJobMessage,
)
from local_qwen_worker.schemas import (
    ChatMessage,
    ChatResult,
)


# Ces tests utilisent les sessions Django.
pytestmark = pytest.mark.django_db


# Identifiants stables utilisés pour rendre
# les résultats des tests faciles à suivre.
FIRST_JOB_ID = UUID(
    "11111111-1111-1111-1111-111111111111"
)

SECOND_JOB_ID = UUID(
    "22222222-2222-2222-2222-222222222222"
)


# RÔLE :
# Remplace QwenService tout en conservant
# les conversations réellement transmises par le worker.
#
# La classe reçoit une séquence de résultats :
#
# - ChatResult :
#   génération réussie ;
#
# - Exception :
#   génération échouée.
class SequencedQwenService:
    def __init__(
        self,
        outcomes: Sequence[
            ChatResult | Exception
        ],
    ) -> None:
        self.outcomes = list(outcomes)

        self.calls: list[
            tuple[
                list[ChatMessage],
                str | None,
            ]
        ] = []

    # RÔLE :
    # Simule la génération Qwen synchrone.
    #
    # APPELÉE PAR :
    # - RabbitMQWorker._generate_response()
    #
    # MODIFIE :
    # - self.calls
    #
    # RETOURNE :
    # - le prochain ChatResult configuré.
    #
    # ERREUR :
    # - lève la prochaine exception configurée.
    def chat(
        self,
        messages: Sequence[ChatMessage],
        system_prompt: str | None = None,
    ) -> ChatResult:
        copied_messages = [
            message.model_copy(deep=True)
            for message in messages
        ]

        self.calls.append(
            (
                copied_messages,
                system_prompt,
            )
        )

        outcome_index = len(self.calls) - 1

        if outcome_index >= len(self.outcomes):
            raise AssertionError(
                "Le faux service Qwen ne contient "
                "plus de résultat configuré."
            )

        outcome = self.outcomes[
            outcome_index
        ]

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


# RÔLE :
# Remplace RabbitMQ par une liaison directe
# entre FastAPI et RabbitMQWorker.
#
# Le broker conserve les mêmes frontières :
#
# - ticket API sérialisé puis validé
#   par le contrat du worker ;
#
# - événement worker sérialisé puis validé
#   par le contrat de l'API.
#
# Cela vérifie que les deux services utilisent
# réellement des contrats compatibles.
class InMemoryRabbitMQBroker:
    def __init__(
        self,
        *,
        job_store: JobStore,
        qwen_service: SequencedQwenService,
    ) -> None:
        self.job_store = job_store
        self.qwen_service = qwen_service

        self.connected = False

        self.published_jobs: list[
            ApiWorkerJobMessage
        ] = []

        self.published_events: list[
            RuntimeWorkerEventMessage
        ] = []

        worker_settings = WorkerSettings(
            qwen_model="integration-qwen-model",
            qwen_context=4096,
            qwen_temperature=0.2,
            ollama_base_url=(
                "http://127.0.0.1:11434"
            ),
            ollama_timeout_seconds=30,
            ollama_keep_alive="5m",
            rabbitmq_url=(
                "amqp://guest:guest@127.0.0.1:5672/"
            ),
            rabbitmq_job_queue=(
                "integration.jobs"
            ),
            rabbitmq_event_queue=(
                "integration.events"
            ),
            _env_file=None,
        )

        self.worker = RabbitMQWorker(
            settings=worker_settings
        )

        # Le worker utilise ici le faux service
        # plutôt que le véritable client Ollama.
        self.worker.qwen_service = (
            qwen_service
        )

        # Les événements du worker sont redirigés
        # vers le JobStore de FastAPI.
        self.worker.publish_event = (
            self._receive_worker_event
        )

    # RÔLE :
    # Simule l'ouverture de RabbitMQ
    # pendant le lifespan FastAPI.
    async def connect(self) -> None:
        self.connected = True

    # RÔLE :
    # Simule la fermeture de RabbitMQ.
    async def close(self) -> None:
        self.connected = False

    # RÔLE :
    # Fournit l'état utilisé par GET /health.
    def is_connected(self) -> bool:
        return self.connected

    # RÔLE :
    # Reçoit le contrat de ticket produit par FastAPI,
    # le convertit dans le contrat du worker
    # puis lance son traitement réel.
    async def publish_job(
        self,
        job: ApiWorkerJobMessage,
    ) -> None:
        self.published_jobs.append(
            job.model_copy(deep=True)
        )

        runtime_job = (
            RuntimeWorkerJobMessage
            .model_validate_json(
                job.model_dump_json()
            )
        )

        await self.worker.process_job(
            runtime_job
        )

    # RÔLE :
    # Reçoit un événement produit par le worker,
    # le valide avec le contrat FastAPI
    # puis l'applique au vrai JobStore.
    async def _receive_worker_event(
        self,
        event: RuntimeWorkerEventMessage,
    ) -> None:
        self.published_events.append(
            event.model_copy(deep=True)
        )

        api_event = (
            ApiWorkerEventMessage
            .model_validate_json(
                event.model_dump_json()
            )
        )

        await self.job_store.apply_event(
            api_event
        )


# RÔLE :
# Regroupe les objets utiles
# pendant un scénario d'intégration.
@dataclass(slots=True)
class IntegrationEnvironment:
    django_client: Client
    api_client: TestClient

    job_store: JobStore
    broker: InMemoryRabbitMQBroker
    qwen_service: SequencedQwenService


# RÔLE :
# Crée l'environnement complet d'intégration.
#
# MODIFIE TEMPORAIREMENT :
# - app.main.job_store ;
# - app.main.broker ;
# - chat.services._request_json.
#
# Le transport réseau de chat.services est remplacé
# par un appel direct au client ASGI de FastAPI.
@contextmanager
def _run_integration_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: Sequence[
        ChatResult | Exception
    ],
) -> Iterator[IntegrationEnvironment]:
    job_store = JobStore()

    qwen_service = SequencedQwenService(
        outcomes
    )

    broker = InMemoryRabbitMQBroker(
        job_store=job_store,
        qwen_service=qwen_service,
    )

    monkeypatch.setattr(
        main,
        "job_store",
        job_store,
    )

    monkeypatch.setattr(
        main,
        "broker",
        broker,
    )

    with TestClient(main.app) as api_client:
        # RÔLE :
        # Remplace uniquement le transport HTTPX.
        #
        # Les fonctions publiques :
        #
        # - chat.services.create_job()
        # - chat.services.get_job_status()
        #
        # restent réellement exécutées.
        def request_json_through_asgi(
            method: str,
            path: str,
            *,
            json_body: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            request_arguments: dict[
                str,
                Any,
            ] = {
                "headers": {
                    "Accept": "application/json",
                },
            }

            if json_body is not None:
                request_arguments["json"] = (
                    json_body
                )

            response = api_client.request(
                method,
                path,
                **request_arguments,
            )

            if not response.is_success:
                raise ApiClientError(
                    front_services
                    ._extract_error_message(
                        response
                    )
                )

            try:
                payload: Any = response.json()

            except ValueError as exc:
                raise ApiClientError(
                    "FastAPI n'a pas retourné "
                    "une réponse JSON valide."
                ) from exc

            if not isinstance(payload, dict):
                raise ApiClientError(
                    "La réponse JSON de FastAPI "
                    "ne possède pas la structure "
                    "attendue."
                )

            return payload

        monkeypatch.setattr(
            front_services,
            "_request_json",
            request_json_through_asgi,
        )

        yield IntegrationEnvironment(
            django_client=Client(),
            api_client=api_client,
            job_store=job_store,
            broker=broker,
            qwen_service=qwen_service,
        )


# RÔLE :
# Envoie une question par la vraie vue Django.
#
# RETOURNE :
# - le JSON HTTP 202 de submit_job().
def _submit_message(
    environment: IntegrationEnvironment,
    message: str,
) -> dict[str, Any]:
    response = (
        environment
        .django_client
        .post(
            reverse(
                "chat:submit-job"
            ),
            data={
                "message": message,
            },
            HTTP_ACCEPT=(
                "application/json"
            ),
        )
    )

    assert response.status_code == 202

    payload: dict[str, Any] = (
        response.json()
    )

    return payload


# RÔLE :
# Demande à la vraie vue Django
# de lire et finaliser le job actif.
#
# RETOURNE :
# - le JSON de job_status().
def _read_job_status(
    environment: IntegrationEnvironment,
    status_url: str,
) -> dict[str, Any]:
    response = (
        environment
        .django_client
        .get(
            status_url,
            HTTP_ACCEPT=(
                "application/json"
            ),
        )
    )

    assert response.status_code == 200

    payload: dict[str, Any] = (
        response.json()
    )

    return payload


# ---------------------------------------------------------------------------
# Pipeline complet réussi
# ---------------------------------------------------------------------------


# Vérifie le trajet complet d'une réponse réussie :
#
# Django
# → FastAPI
# → broker en mémoire
# → worker
# → faux Qwen
# → completed
# → FastAPI
# → Django
# → historique de session.
def test_successful_chat_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _run_integration_environment(
        monkeypatch,
        outcomes=[
            ChatResult(
                content=(
                    "Une frégate est un navire "
                    "militaire polyvalent."
                ),
                model=(
                    "integration-qwen-model"
                ),
            )
        ],
    ) as environment:
        monkeypatch.setattr(
            main,
            "uuid4",
            lambda: FIRST_JOB_ID,
        )

        created_payload = _submit_message(
            environment,
            (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        )

        assert created_payload["job_id"] == (
            str(FIRST_JOB_ID)
        )

        # Le broker en mémoire traite immédiatement
        # le ticket avant le retour de FastAPI.
        assert created_payload["state"] == (
            "completed"
        )

        session = (
            environment
            .django_client
            .session
        )

        assert session[
            ACTIVE_JOB_SESSION_KEY
        ] == str(FIRST_JOB_ID)

        assert session[
            HISTORY_SESSION_KEY
        ] == [
            {
                "role": "user",
                "content": (
                    "Explique le fonctionnement "
                    "d'une frégate."
                ),
            }
        ]

        status_payload = _read_job_status(
            environment,
            created_payload["status_url"],
        )

        assert status_payload["state"] == (
            "completed"
        )

        assert status_payload["reload"] is True

        session = (
            environment
            .django_client
            .session
        )

        assert session[
            HISTORY_SESSION_KEY
        ] == [
            {
                "role": "user",
                "content": (
                    "Explique le fonctionnement "
                    "d'une frégate."
                ),
            },
            {
                "role": "assistant",
                "content": (
                    "Une frégate est un navire "
                    "militaire polyvalent."
                ),
            },
        ]

        assert session[
            MODEL_SESSION_KEY
        ] == "integration-qwen-model"

        assert (
            ACTIVE_JOB_SESSION_KEY
            not in session
        )

        assert len(
            environment
            .broker
            .published_jobs
        ) == 1

        assert [
            event.state.value
            for event
            in (
                environment
                .broker
                .published_events
            )
        ] == [
            "running",
            "completed",
        ]

        assert len(
            environment
            .qwen_service
            .calls
        ) == 1

        messages, system_prompt = (
            environment
            .qwen_service
            .calls[0]
        )

        assert messages[-1].role == "user"

        assert system_prompt == (
            DEFAULT_SYSTEM_PROMPT
        )


# ---------------------------------------------------------------------------
# Pipeline complet en échec
# ---------------------------------------------------------------------------


# Vérifie qu'une erreur contrôlée d'Ollama :
#
# - devient un événement failed ;
# - remonte jusqu'à Django ;
# - retire le job actif ;
# - n'ajoute aucune fausse réponse assistant.
def test_failed_chat_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _run_integration_environment(
        monkeypatch,
        outcomes=[
            OllamaUnavailableError(
                "Ollama simulé est indisponible."
            )
        ],
    ) as environment:
        monkeypatch.setattr(
            main,
            "uuid4",
            lambda: FIRST_JOB_ID,
        )

        created_payload = _submit_message(
            environment,
            "Explique les sous-marins.",
        )

        assert created_payload["state"] == (
            "failed"
        )

        status_payload = _read_job_status(
            environment,
            created_payload["status_url"],
        )

        assert status_payload["state"] == (
            "failed"
        )

        assert status_payload["error"] == (
            "Ollama simulé est indisponible."
        )

        assert status_payload["reload"] is False

        session = (
            environment
            .django_client
            .session
        )

        assert session[
            HISTORY_SESSION_KEY
        ] == [
            {
                "role": "user",
                "content": (
                    "Explique les sous-marins."
                ),
            }
        ]

        assert (
            ACTIVE_JOB_SESSION_KEY
            not in session
        )

        assert (
            MODEL_SESSION_KEY
            not in session
        )

        assert [
            event.state.value
            for event
            in (
                environment
                .broker
                .published_events
            )
        ] == [
            "running",
            "failed",
        ]


# ---------------------------------------------------------------------------
# Conversation en plusieurs tours
# ---------------------------------------------------------------------------


# Vérifie qu'après une première réponse,
# le second ticket contient bien :
#
# - la première question ;
# - la première réponse ;
# - la nouvelle question.
def test_multi_turn_chat_pipeline_preserves_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _run_integration_environment(
        monkeypatch,
        outcomes=[
            ChatResult(
                content=(
                    "Une corvette est généralement "
                    "plus petite qu'une frégate."
                ),
                model=(
                    "integration-qwen-model"
                ),
            ),
            ChatResult(
                content=(
                    "La frégate possède généralement "
                    "une autonomie supérieure."
                ),
                model=(
                    "integration-qwen-model"
                ),
            ),
        ],
    ) as environment:
        job_identifiers = iter(
            [
                FIRST_JOB_ID,
                SECOND_JOB_ID,
            ]
        )

        monkeypatch.setattr(
            main,
            "uuid4",
            lambda: next(
                job_identifiers
            ),
        )

        first_payload = _submit_message(
            environment,
            (
                "Quelle différence entre "
                "une corvette et une frégate ?"
            ),
        )

        first_status = _read_job_status(
            environment,
            first_payload["status_url"],
        )

        assert first_status["state"] == (
            "completed"
        )

        second_payload = _submit_message(
            environment,
            (
                "Laquelle possède généralement "
                "la meilleure autonomie ?"
            ),
        )

        second_status = _read_job_status(
            environment,
            second_payload["status_url"],
        )

        assert second_status["state"] == (
            "completed"
        )

        assert len(
            environment
            .qwen_service
            .calls
        ) == 2

        second_messages, second_prompt = (
            environment
            .qwen_service
            .calls[1]
        )

        assert [
            message.role
            for message
            in second_messages
        ] == [
            "user",
            "assistant",
            "user",
        ]

        assert [
            message.content
            for message
            in second_messages
        ] == [
            (
                "Quelle différence entre "
                "une corvette et une frégate ?"
            ),
            (
                "Une corvette est généralement "
                "plus petite qu'une frégate."
            ),
            (
                "Laquelle possède généralement "
                "la meilleure autonomie ?"
            ),
        ]

        assert second_prompt == (
            DEFAULT_SYSTEM_PROMPT
        )

        session = (
            environment
            .django_client
            .session
        )

        assert len(
            session[
                HISTORY_SESSION_KEY
            ]
        ) == 4

        assert (
            session[
                HISTORY_SESSION_KEY
            ][-1]
        ) == {
            "role": "assistant",
            "content": (
                "La frégate possède généralement "
                "une autonomie supérieure."
            ),
        }

        assert (
            ACTIVE_JOB_SESSION_KEY
            not in session
        )