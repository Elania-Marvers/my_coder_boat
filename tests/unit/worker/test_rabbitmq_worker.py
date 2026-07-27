"""
FICHIER :
tests/unit/worker/test_rabbitmq_worker.py

RÔLE GÉNÉRAL :
Teste le traitement des tickets par RabbitMQWorker
sans démarrer de véritable broker RabbitMQ
et sans contacter Ollama.

Les tests remplacent :

- QwenService par un faux service synchrone ;
- RabbitMQ par de faux messages et un faux canal ;
- publish_event() par AsyncMock lorsque nécessaire.

CIRCULATION TESTÉE :

message RabbitMQ
→ RabbitMQWorker._handle_job_message()
→ RabbitMQWorker.process_job()
→ faux QwenService
→ événement running, completed ou failed
→ ack, reject ou nack

ÉLÉMENTS TESTÉS :

- publication de running puis completed ;
- erreur Qwen contrôlée ;
- erreur inattendue ;
- résultat trop long ;
- nom de modèle trop long ;
- nettoyage des erreurs ;
- validation du JSON RabbitMQ ;
- rejet d'un ticket invalide ;
- acquittement d'un ticket réussi ;
- remise en file après une panne technique ;
- publication dans la bonne file d'événements ;
- protection lorsque le canal est absent ou fermé.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_JOB_CONSUME
- CHAT_JOB_GENERATE
- CHAT_JOB_EVENT
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aio_pika import DeliveryMode

from local_qwen_worker.config import WorkerSettings
from local_qwen_worker.ollama_client import (
    QwenWorkerError,
)
from local_qwen_worker.prompts import (
    DEFAULT_SYSTEM_PROMPT,
)
from local_qwen_worker.rabbitmq_worker import (
    MAX_ERROR_LENGTH,
    MAX_MODEL_NAME_LENGTH,
    MAX_RESULT_LENGTH,
    RabbitMQWorker,
    WorkerEventMessage,
    WorkerEventState,
    WorkerJobMessage,
)
from local_qwen_worker.schemas import (
    ChatMessage,
    ChatResult,
)


# Identifiant stable utilisé par tous les tests.
JOB_ID = UUID(
    "12345678-1234-5678-1234-567812345678"
)


# Date fixe utilisée pour les événements RabbitMQ.
REFERENCE_TIME = datetime(
    2026,
    7,
    26,
    14,
    0,
    0,
    tzinfo=UTC,
)


# RÔLE :
# Simule QwenService sans contacter Ollama.
#
# Le faux service peut :
#
# - retourner un ChatResult ;
# - lever une exception contrôlée ;
# - lever une exception inattendue ;
# - conserver les appels reçus.
class RecordingQwenService:
    def __init__(
        self,
        *,
        result: ChatResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error

        self.calls: list[
            tuple[
                list[ChatMessage],
                str | None,
            ]
        ] = []

    # RÔLE :
    # Simule QwenService.chat().
    #
    # APPELÉE PAR :
    # - RabbitMQWorker._generate_response()
    #
    # MODIFIE :
    # - self.calls
    #
    # RETOURNE :
    # - le ChatResult configuré.
    #
    # ERREUR :
    # - lève l'exception configurée.
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

        if self.error is not None:
            raise self.error

        if self.result is None:
            raise AssertionError(
                "Le faux QwenService ne contient "
                "ni résultat ni erreur."
            )

        return self.result


# RÔLE :
# Simule un message livré par RabbitMQ.
#
# Cette classe conserve les décisions prises
# par RabbitMQWorker :
#
# - ack ;
# - reject ;
# - nack.
class FakeIncomingMessage:
    def __init__(
        self,
        body: bytes,
    ) -> None:
        self.body = body

        self.ack_count = 0

        self.reject_requeue_values: list[
            bool
        ] = []

        self.nack_requeue_values: list[
            bool
        ] = []

    # RÔLE :
    # Simule la suppression définitive
    # d'un ticket traité.
    async def ack(self) -> None:
        self.ack_count += 1

    # RÔLE :
    # Simule le rejet d'un ticket invalide.
    async def reject(
        self,
        *,
        requeue: bool,
    ) -> None:
        self.reject_requeue_values.append(
            requeue
        )

    # RÔLE :
    # Simule la remise en file d'un ticket
    # après une erreur temporaire.
    async def nack(
        self,
        *,
        requeue: bool,
    ) -> None:
        self.nack_requeue_values.append(
            requeue
        )


# RÔLE :
# Simule l'exchange RabbitMQ par défaut.
#
# MODIFIE :
# - self.publications
class FakeExchange:
    def __init__(self) -> None:
        self.publications: list[
            tuple[Any, str]
        ] = []

    async def publish(
        self,
        message: Any,
        *,
        routing_key: str,
    ) -> None:
        self.publications.append(
            (
                message,
                routing_key,
            )
        )


# RÔLE :
# Simule un canal RabbitMQ ouvert.
class FakeChannel:
    def __init__(
        self,
        *,
        is_closed: bool = False,
    ) -> None:
        self.is_closed = is_closed
        self.default_exchange = FakeExchange()


# RÔLE :
# Fournit une configuration indépendante
# du fichier .env réel.
@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        qwen_model="qwen-test-model",
        qwen_context=4096,
        qwen_temperature=0.15,
        ollama_base_url=(
            "http://127.0.0.1:11434"
        ),
        ollama_timeout_seconds=30.0,
        ollama_keep_alive="5m",
        rabbitmq_url=(
            "amqp://guest:guest@127.0.0.1:5672/"
        ),
        rabbitmq_job_queue="test.jobs",
        rabbitmq_event_queue="test.events",
    )


# RÔLE :
# Force pytest-anyio à utiliser asyncio
# pour les tests asynchrones.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# RÔLE :
# Construit un ticket RabbitMQ valide.
def _build_job() -> WorkerJobMessage:
    return WorkerJobMessage(
        job_id=JOB_ID,
        messages=[
            ChatMessage(
                role="user",
                content=(
                    "Explique le fonctionnement "
                    "d'une frégate."
                ),
            )
        ],
    )


# RÔLE :
# Construit un événement running valide.
def _build_running_event() -> WorkerEventMessage:
    return WorkerEventMessage(
        job_id=JOB_ID,
        state=WorkerEventState.RUNNING,
        occurred_at=REFERENCE_TIME,
        progress_percent=None,
    )


# RÔLE :
# Crée RabbitMQWorker avec le faux service fourni.
#
# Le remplacement est réalisé avant l'appel
# au constructeur afin qu'aucun OllamaClient réel
# ne soit créé.
def _build_worker(
    monkeypatch: pytest.MonkeyPatch,
    settings: WorkerSettings,
    service: RecordingQwenService,
) -> RabbitMQWorker:
    monkeypatch.setattr(
        (
            "local_qwen_worker.rabbitmq_worker."
            "QwenService"
        ),
        lambda: service,
    )

    return RabbitMQWorker(
        settings=settings
    )


# RÔLE :
# Retourne les événements transmis
# à un AsyncMock remplaçant publish_event().
def _get_published_events(
    publish_mock: AsyncMock,
) -> list[WorkerEventMessage]:
    return [
        awaited_call.args[0]
        for awaited_call
        in publish_mock.await_args_list
    ]


# ---------------------------------------------------------------------------
# Traitement réussi
# ---------------------------------------------------------------------------


# Vérifie le chemin normal :
#
# running → génération → completed.
@pytest.mark.anyio
async def test_process_job_publishes_running_then_completed(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse finale de Qwen.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    publish_mock = AsyncMock()
    worker.publish_event = publish_mock

    await worker.process_job(
        _build_job()
    )

    events = _get_published_events(
        publish_mock
    )

    assert len(events) == 2

    running_event = events[0]
    completed_event = events[1]

    assert running_event.job_id == JOB_ID
    assert running_event.state == (
        WorkerEventState.RUNNING
    )

    assert completed_event.job_id == JOB_ID
    assert completed_event.state == (
        WorkerEventState.COMPLETED
    )

    assert completed_event.progress_percent == 100
    assert completed_event.content == (
        "Réponse finale de Qwen."
    )
    assert completed_event.model == (
        "qwen-test-model"
    )
    assert completed_event.error is None

    assert len(service.calls) == 1

    received_messages, system_prompt = (
        service.calls[0]
    )

    assert received_messages[-1].role == "user"

    assert system_prompt == (
        DEFAULT_SYSTEM_PROMPT
    )


# ---------------------------------------------------------------------------
# Erreurs produites pendant la génération
# ---------------------------------------------------------------------------


# Vérifie qu'une erreur Qwen contrôlée
# devient un événement failed lisible.
@pytest.mark.anyio
async def test_process_job_publishes_failed_for_qwen_error(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        error=QwenWorkerError(
            "Ollama est indisponible."
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    publish_mock = AsyncMock()
    worker.publish_event = publish_mock

    await worker.process_job(
        _build_job()
    )

    events = _get_published_events(
        publish_mock
    )

    assert [
        event.state
        for event in events
    ] == [
        WorkerEventState.RUNNING,
        WorkerEventState.FAILED,
    ]

    failed_event = events[-1]

    assert failed_event.error == (
        "Ollama est indisponible."
    )

    assert failed_event.content is None
    assert failed_event.model is None
    assert failed_event.progress_percent is None


# Vérifie qu'une erreur inattendue
# ne révèle pas son détail technique au front.
@pytest.mark.anyio
async def test_process_job_publishes_generic_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        error=RuntimeError(
            "Détail technique sensible."
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    publish_mock = AsyncMock()
    worker.publish_event = publish_mock

    await worker.process_job(
        _build_job()
    )

    events = _get_published_events(
        publish_mock
    )

    assert events[-1].state == (
        WorkerEventState.FAILED
    )

    assert events[-1].error == (
        "Une erreur inattendue est survenue "
        "pendant la génération Qwen."
    )

    assert "sensible" not in events[-1].error


# ---------------------------------------------------------------------------
# Validation du résultat Qwen
# ---------------------------------------------------------------------------


# Vérifie qu'une réponse dépassant
# la limite contractuelle devient failed.
@pytest.mark.anyio
async def test_process_job_rejects_oversized_content(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content=(
                "x"
                * (MAX_RESULT_LENGTH + 1)
            ),
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    publish_mock = AsyncMock()
    worker.publish_event = publish_mock

    await worker.process_job(
        _build_job()
    )

    events = _get_published_events(
        publish_mock
    )

    assert events[-1].state == (
        WorkerEventState.FAILED
    )

    assert events[-1].error is not None
    assert "dépasse" in events[-1].error


# Vérifie qu'un nom de modèle trop long
# ne peut pas être envoyé à FastAPI.
@pytest.mark.anyio
async def test_process_job_rejects_oversized_model_name(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse valide.",
            model=(
                "m"
                * (MAX_MODEL_NAME_LENGTH + 1)
            ),
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    publish_mock = AsyncMock()
    worker.publish_event = publish_mock

    await worker.process_job(
        _build_job()
    )

    events = _get_published_events(
        publish_mock
    )

    assert events[-1].state == (
        WorkerEventState.FAILED
    )

    assert events[-1].error is not None
    assert "nom du modèle" in events[-1].error
    assert "dépasse" in events[-1].error


# ---------------------------------------------------------------------------
# Nettoyage des erreurs
# ---------------------------------------------------------------------------


# Vérifie qu'une erreur vide reçoit
# un message générique non vide.
def test_normalise_error_message_replaces_blank_error() -> None:
    error = RabbitMQWorker._normalise_error_message(
        "   "
    )

    assert error == (
        "Le worker a rencontré une erreur "
        "sans fournir de détail."
    )


# Vérifie qu'une erreur trop longue
# est limitée au contrat FastAPI.
def test_normalise_error_message_truncates_long_error() -> None:
    raw_error = "x" * (
        MAX_ERROR_LENGTH + 500
    )

    error = RabbitMQWorker._normalise_error_message(
        raw_error
    )

    assert len(error) == MAX_ERROR_LENGTH
    assert error == (
        "x" * MAX_ERROR_LENGTH
    )


# ---------------------------------------------------------------------------
# Validation du ticket RabbitMQ
# ---------------------------------------------------------------------------


# Vérifie qu'un ticket JSON valide
# est transformé en WorkerJobMessage.
def test_parse_job_message_accepts_valid_json() -> None:
    expected_job = _build_job()

    message = FakeIncomingMessage(
        expected_job
        .model_dump_json()
        .encode("utf-8")
    )

    parsed_job = (
        RabbitMQWorker
        ._parse_job_message(message)
    )

    assert parsed_job == expected_job


# Vérifie qu'un JSON illisible est refusé.
def test_parse_job_message_rejects_invalid_json() -> None:
    message = FakeIncomingMessage(
        b"{json-invalide"
    )

    parsed_job = (
        RabbitMQWorker
        ._parse_job_message(message)
    )

    assert parsed_job is None


# Vérifie qu'un ticket provenant de FastAPI
# ne peut pas imposer son propre prompt système.
def test_parse_job_message_rejects_system_message() -> None:
    payload = {
        "job_id": str(JOB_ID),
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
    }

    message = FakeIncomingMessage(
        json.dumps(payload).encode("utf-8")
    )

    parsed_job = (
        RabbitMQWorker
        ._parse_job_message(message)
    )

    assert parsed_job is None


# ---------------------------------------------------------------------------
# Décisions ack, reject et nack
# ---------------------------------------------------------------------------


# Vérifie qu'un ticket illisible
# est rejeté sans remise en file.
@pytest.mark.anyio
async def test_handle_invalid_message_rejects_without_requeue(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    message = FakeIncomingMessage(
        b"message-invalide"
    )

    await worker._handle_job_message(
        message
    )

    assert message.reject_requeue_values == [
        False,
    ]

    assert message.ack_count == 0
    assert message.nack_requeue_values == []


# Vérifie qu'un ticket traité complètement
# est acquitté une seule fois.
@pytest.mark.anyio
async def test_handle_successful_message_acknowledges_ticket(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    process_mock = AsyncMock(
        return_value=None
    )

    worker.process_job = process_mock

    job = _build_job()

    message = FakeIncomingMessage(
        job.model_dump_json().encode(
            "utf-8"
        )
    )

    await worker._handle_job_message(
        message
    )

    process_mock.assert_awaited_once()

    assert message.ack_count == 1
    assert message.reject_requeue_values == []
    assert message.nack_requeue_values == []


# Vérifie qu'une panne technique pendant
# la publication des événements remet le ticket en file.
@pytest.mark.anyio
async def test_handle_infrastructure_failure_nacks_with_requeue(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    worker.process_job = AsyncMock(
        side_effect=RuntimeError(
            "RabbitMQ indisponible."
        )
    )

    job = _build_job()

    message = FakeIncomingMessage(
        job.model_dump_json().encode(
            "utf-8"
        )
    )

    await worker._handle_job_message(
        message
    )

    assert message.nack_requeue_values == [
        True,
    ]

    assert message.ack_count == 0
    assert message.reject_requeue_values == []


# ---------------------------------------------------------------------------
# Publication d'un événement RabbitMQ
# ---------------------------------------------------------------------------


# Vérifie que publish_event() utilise
# la file d'événements configurée.
@pytest.mark.anyio
async def test_publish_event_uses_event_queue(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    fake_channel = FakeChannel()
    worker.channel = fake_channel

    event = _build_running_event()

    await worker.publish_event(event)

    publications = (
        fake_channel
        .default_exchange
        .publications
    )

    assert len(publications) == 1

    message, routing_key = publications[0]

    assert routing_key == "test.events"

    assert message.message_id == str(JOB_ID)

    assert message.correlation_id == (
        str(JOB_ID)
    )

    assert message.content_type == (
        "application/json"
    )

    assert message.delivery_mode == (
        DeliveryMode.PERSISTENT
    )

    payload = json.loads(
        message.body.decode("utf-8")
    )

    assert payload["job_id"] == str(JOB_ID)
    assert payload["state"] == "running"
    assert payload["progress_percent"] is None


# ---------------------------------------------------------------------------
# Protection du canal RabbitMQ
# ---------------------------------------------------------------------------


# Vérifie qu'une publication ne peut pas commencer
# avant l'ouverture du canal.
def test_get_channel_raises_when_channel_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    worker.channel = None

    with pytest.raises(
        RuntimeError,
        match="n'est pas disponible",
    ):
        worker._get_channel_or_raise()


# Vérifie qu'un ancien canal fermé
# ne peut pas être réutilisé.
def test_get_channel_raises_when_channel_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    service = RecordingQwenService(
        result=ChatResult(
            content="Réponse.",
            model="qwen-test-model",
        )
    )

    worker = _build_worker(
        monkeypatch,
        worker_settings,
        service,
    )

    worker.channel = FakeChannel(
        is_closed=True
    )

    with pytest.raises(
        RuntimeError,
        match="n'est pas disponible",
    ):
        worker._get_channel_or_raise()