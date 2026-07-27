"""
FICHIER :
tests/unit/api/test_broker.py

RÔLE GÉNÉRAL :
Teste le broker RabbitMQ utilisé par FastAPI.

Aucun véritable serveur RabbitMQ n'est démarré.

Les tests utilisent :

- une fausse connexion RabbitMQ ;
- un faux canal AMQP ;
- de fausses files ;
- un faux exchange ;
- de faux messages entrants ;
- un faux JobStore.

CIRCULATION TESTÉE :

FastAPI
→ RabbitMQBroker.publish_job()
→ faux exchange RabbitMQ

et :

faux événement RabbitMQ
→ RabbitMQBroker._handle_event()
→ faux JobStore
→ ack, reject ou nack

ÉLÉMENTS TESTÉS :

- construction d'un message de job ;
- déclaration des deux files ;
- démarrage du consommateur d'événements ;
- publication dans la bonne file ;
- validation des événements JSON ;
- rejet définitif d'un événement invalide ;
- acquittement d'un événement valide ;
- remise en file après une erreur temporaire ;
- ouverture et fermeture de la connexion ;
- détection de l'état de connexion ;
- protection contre un canal absent ou fermé.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- STARTUP
- CHAT_JOB_PUBLISH
- CHAT_JOB_EVENT
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aio_pika import DeliveryMode

from app import broker as broker_module
from app.broker import (
    JOB_QUEUE_ARGUMENTS,
    RabbitMQBroker,
)
from app.config import ApiSettings
from app.schemas import (
    JobState,
    WorkerEventMessage,
    WorkerJobMessage,
)


# Identifiant stable utilisé par les tests.
JOB_ID = UUID(
    "12345678-1234-5678-1234-567812345678"
)


# Date fixe avec fuseau horaire.
REFERENCE_TIME = datetime(
    2026,
    7,
    27,
    10,
    0,
    0,
    tzinfo=UTC,
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

    # RÔLE :
    # Enregistre une publication AMQP.
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
# Simule une file RabbitMQ.
#
# Cette classe conserve les appels
# effectués à consume().
class FakeQueue:
    def __init__(
        self,
        name: str,
    ) -> None:
        self.name = name

        self.consume_calls: list[
            dict[str, Any]
        ] = []

    # RÔLE :
    # Simule l'enregistrement d'un consommateur.
    async def consume(
        self,
        callback: Any,
        *,
        no_ack: bool,
    ) -> str:
        self.consume_calls.append(
            {
                "callback": callback,
                "no_ack": no_ack,
            }
        )

        return "fake-consumer-tag"


# RÔLE :
# Simule un canal RabbitMQ.
#
# RESPONSABILITÉS :
# - déclarer les files ;
# - exposer le default exchange ;
# - indiquer si le canal est fermé.
class FakeChannel:
    def __init__(
        self,
        *,
        is_closed: bool = False,
    ) -> None:
        self.is_closed = is_closed
        self.default_exchange = FakeExchange()

        self.declare_queue_calls: list[
            dict[str, Any]
        ] = []

        self.queues: dict[
            str,
            FakeQueue,
        ] = {}

    # RÔLE :
    # Simule channel.declare_queue().
    async def declare_queue(
        self,
        name: str,
        *,
        durable: bool,
        arguments: dict[str, bool] | None = None,
    ) -> FakeQueue:
        self.declare_queue_calls.append(
            {
                "name": name,
                "durable": durable,
                "arguments": arguments,
            }
        )

        queue = self.queues.get(name)

        if queue is None:
            queue = FakeQueue(name)
            self.queues[name] = queue

        return queue


# RÔLE :
# Simule une connexion robuste RabbitMQ.
class FakeConnection:
    def __init__(
        self,
        *,
        is_closed: bool = False,
        channel: FakeChannel | None = None,
    ) -> None:
        self.is_closed = is_closed

        self.channel_object = (
            channel
            if channel is not None
            else FakeChannel()
        )

        self.channel_calls: list[bool] = []
        self.close_count = 0

    # RÔLE :
    # Simule connection.channel().
    async def channel(
        self,
        *,
        publisher_confirms: bool,
    ) -> FakeChannel:
        self.channel_calls.append(
            publisher_confirms
        )

        return self.channel_object

    # RÔLE :
    # Simule la fermeture de la connexion.
    async def close(self) -> None:
        self.close_count += 1
        self.is_closed = True


# RÔLE :
# Simule JobStore.apply_event().
#
# Le faux stockage peut :
#
# - enregistrer les événements reçus ;
# - lever une exception temporaire.
class FakeJobStore:
    def __init__(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        self.error = error

        self.applied_events: list[
            WorkerEventMessage
        ] = []

    # RÔLE :
    # Simule JobStore.apply_event().
    async def apply_event(
        self,
        event: WorkerEventMessage,
    ) -> None:
        if self.error is not None:
            raise self.error

        self.applied_events.append(event)


# RÔLE :
# Simule un message entrant RabbitMQ.
#
# La classe conserve les décisions :
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

    async def ack(self) -> None:
        self.ack_count += 1

    async def reject(
        self,
        *,
        requeue: bool,
    ) -> None:
        self.reject_requeue_values.append(
            requeue
        )

    async def nack(
        self,
        *,
        requeue: bool,
    ) -> None:
        self.nack_requeue_values.append(
            requeue
        )


# RÔLE :
# Fournit une configuration indépendante
# du fichier .env local.
@pytest.fixture
def api_settings() -> ApiSettings:
    return ApiSettings(
        rabbitmq_url=(
            "amqp://guest:guest@127.0.0.1:5672/"
        ),
        rabbitmq_job_queue="test.jobs",
        rabbitmq_event_queue="test.events",
    )


# RÔLE :
# Force pytest-anyio à utiliser asyncio.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# RÔLE :
# Construit un ticket valide destiné au worker.
def _build_job() -> WorkerJobMessage:
    return WorkerJobMessage(
        job_id=JOB_ID,
        messages=[
            {
                "role": "user",
                "content": (
                    "Explique le fonctionnement "
                    "d'une frégate."
                ),
            }
        ],
    )


# RÔLE :
# Construit un événement running valide.
def _build_running_event() -> WorkerEventMessage:
    return WorkerEventMessage(
        job_id=JOB_ID,
        state=JobState.RUNNING,
        occurred_at=REFERENCE_TIME,
        progress_percent=None,
    )


# RÔLE :
# Construit un broker avec ses dépendances
# de test.
def _build_broker(
    settings: ApiSettings,
    *,
    job_store: FakeJobStore | None = None,
) -> RabbitMQBroker:
    return RabbitMQBroker(
        settings=settings,
        job_store=(
            job_store
            if job_store is not None
            else FakeJobStore()
        ),
    )


# ---------------------------------------------------------------------------
# Construction du message de job
# ---------------------------------------------------------------------------


# Vérifie que le ticket Pydantic devient
# un message AMQP persistant et traçable.
def test_build_job_message_serialises_ticket() -> None:
    job = _build_job()

    message = RabbitMQBroker._build_job_message(
        job
    )

    assert message.content_type == (
        "application/json"
    )

    assert message.delivery_mode == (
        DeliveryMode.PERSISTENT
    )

    assert message.message_id == str(JOB_ID)

    assert message.correlation_id == (
        str(JOB_ID)
    )

    payload = json.loads(
        message.body.decode("utf-8")
    )

    assert payload == {
        "job_id": str(JOB_ID),
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


# ---------------------------------------------------------------------------
# Protection du canal
# ---------------------------------------------------------------------------


# Vérifie que le canal doit être ouvert
# avant toute opération RabbitMQ.
def test_get_channel_raises_when_missing(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    rabbitmq_broker.channel = None

    with pytest.raises(
        RuntimeError,
        match="n'est pas disponible",
    ):
        rabbitmq_broker._get_channel_or_raise()


# Vérifie qu'un ancien canal fermé
# ne peut pas être réutilisé.
def test_get_channel_raises_when_closed(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    rabbitmq_broker.channel = FakeChannel(
        is_closed=True
    )

    with pytest.raises(
        RuntimeError,
        match="n'est pas disponible",
    ):
        rabbitmq_broker._get_channel_or_raise()


# ---------------------------------------------------------------------------
# Déclaration des files
# ---------------------------------------------------------------------------


# Vérifie la déclaration de la file
# contenant les demandes du worker.
@pytest.mark.anyio
async def test_declare_job_queue_uses_expected_options(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    channel = FakeChannel()
    rabbitmq_broker.channel = channel

    queue = await (
        rabbitmq_broker
        ._declare_job_queue()
    )

    assert queue.name == "test.jobs"

    assert channel.declare_queue_calls == [
        {
            "name": "test.jobs",
            "durable": True,
            "arguments": JOB_QUEUE_ARGUMENTS,
        }
    ]


# Vérifie la déclaration de la file
# contenant les événements du worker.
@pytest.mark.anyio
async def test_declare_event_queue_uses_expected_options(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    channel = FakeChannel()
    rabbitmq_broker.channel = channel

    queue = await (
        rabbitmq_broker
        ._declare_event_queue()
    )

    assert queue.name == "test.events"

    assert channel.declare_queue_calls == [
        {
            "name": "test.events",
            "durable": True,
            "arguments": None,
        }
    ]


# ---------------------------------------------------------------------------
# Consommation de la file des événements
# ---------------------------------------------------------------------------


# Vérifie que le consommateur ne peut pas
# démarrer avant la déclaration de la file.
@pytest.mark.anyio
async def test_start_event_consumer_requires_queue(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    rabbitmq_broker.event_queue = None

    with pytest.raises(
        RuntimeError,
        match="n'est pas initialisée",
    ):
        await (
            rabbitmq_broker
            ._start_event_consumer()
        )


# Vérifie que la fonction _handle_event
# est enregistrée avec acquittement manuel.
@pytest.mark.anyio
async def test_start_event_consumer_registers_handler(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    event_queue = FakeQueue(
        "test.events"
    )

    rabbitmq_broker.event_queue = event_queue

    await (
        rabbitmq_broker
        ._start_event_consumer()
    )

    assert len(
        event_queue.consume_calls
    ) == 1

    consume_call = (
        event_queue.consume_calls[0]
    )

    assert consume_call["callback"] == (
        rabbitmq_broker._handle_event
    )

    assert consume_call["no_ack"] is False


# ---------------------------------------------------------------------------
# Publication d'un ticket
# ---------------------------------------------------------------------------


# Vérifie que publish_job() utilise
# la file de jobs configurée.
@pytest.mark.anyio
async def test_publish_job_uses_job_queue(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    channel = FakeChannel()
    rabbitmq_broker.channel = channel

    await rabbitmq_broker.publish_job(
        _build_job()
    )

    publications = (
        channel
        .default_exchange
        .publications
    )

    assert len(publications) == 1

    message, routing_key = publications[0]

    assert routing_key == "test.jobs"

    assert message.message_id == str(JOB_ID)

    assert message.correlation_id == (
        str(JOB_ID)
    )


# ---------------------------------------------------------------------------
# Analyse des événements
# ---------------------------------------------------------------------------


# Vérifie qu'un événement JSON valide
# devient un WorkerEventMessage.
def test_parse_event_accepts_valid_json() -> None:
    expected_event = _build_running_event()

    message = FakeIncomingMessage(
        expected_event
        .model_dump_json()
        .encode("utf-8")
    )

    parsed_event = (
        RabbitMQBroker._parse_event(
            message
        )
    )

    assert parsed_event == expected_event


# Vérifie qu'un JSON invalide
# est rejeté pendant son analyse.
def test_parse_event_rejects_invalid_json() -> None:
    message = FakeIncomingMessage(
        b"{json-invalide"
    )

    parsed_event = (
        RabbitMQBroker._parse_event(
            message
        )
    )

    assert parsed_event is None


# ---------------------------------------------------------------------------
# Décisions ack, reject et nack
# ---------------------------------------------------------------------------


# Vérifie qu'un événement invalide
# est rejeté sans remise en file.
@pytest.mark.anyio
async def test_handle_invalid_event_rejects_without_requeue(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    message = FakeIncomingMessage(
        b"evenement-invalide"
    )

    await rabbitmq_broker._handle_event(
        message
    )

    assert message.reject_requeue_values == [
        False,
    ]

    assert message.ack_count == 0

    assert message.nack_requeue_values == []


# Vérifie qu'un événement valide
# est appliqué puis acquitté.
@pytest.mark.anyio
async def test_handle_valid_event_applies_and_acks(
    api_settings: ApiSettings,
) -> None:
    job_store = FakeJobStore()

    rabbitmq_broker = _build_broker(
        api_settings,
        job_store=job_store,
    )

    expected_event = _build_running_event()

    message = FakeIncomingMessage(
        expected_event
        .model_dump_json()
        .encode("utf-8")
    )

    await rabbitmq_broker._handle_event(
        message
    )

    assert job_store.applied_events == [
        expected_event,
    ]

    assert message.ack_count == 1

    assert message.reject_requeue_values == []

    assert message.nack_requeue_values == []


# Vérifie qu'une erreur temporaire du stockage
# replace l'événement dans la file.
@pytest.mark.anyio
async def test_handle_store_failure_nacks_with_requeue(
    api_settings: ApiSettings,
) -> None:
    job_store = FakeJobStore(
        error=RuntimeError(
            "Stockage temporairement indisponible."
        )
    )

    rabbitmq_broker = _build_broker(
        api_settings,
        job_store=job_store,
    )

    event = _build_running_event()

    message = FakeIncomingMessage(
        event
        .model_dump_json()
        .encode("utf-8")
    )

    await rabbitmq_broker._handle_event(
        message
    )

    assert message.nack_requeue_values == [
        True,
    ]

    assert message.ack_count == 0

    assert message.reject_requeue_values == []


# ---------------------------------------------------------------------------
# Détection de l'état de connexion
# ---------------------------------------------------------------------------


# Chaque cas de cette table devient
# un test pytest indépendant.
@pytest.mark.parametrize(
    (
        "connection",
        "channel",
        "expected",
    ),
    [
        (
            None,
            None,
            False,
        ),
        (
            FakeConnection(
                is_closed=True
            ),
            FakeChannel(),
            False,
        ),
        (
            FakeConnection(),
            FakeChannel(
                is_closed=True
            ),
            False,
        ),
        (
            FakeConnection(),
            FakeChannel(),
            True,
        ),
    ],
)
def test_is_connected_requires_open_connection_and_channel(
    api_settings: ApiSettings,
    connection: FakeConnection | None,
    channel: FakeChannel | None,
    expected: bool,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    rabbitmq_broker.connection = connection
    rabbitmq_broker.channel = channel

    assert (
        rabbitmq_broker.is_connected()
        is expected
    )


# ---------------------------------------------------------------------------
# Ouverture et fermeture
# ---------------------------------------------------------------------------


# Vérifie que connect() :
#
# - ouvre la connexion ;
# - crée le canal avec confirmation ;
# - déclare les deux files ;
# - démarre le consommateur.
@pytest.mark.anyio
async def test_connect_initialises_broker(
    api_settings: ApiSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_connection = FakeConnection()

    connect_mock = AsyncMock(
        return_value=fake_connection
    )

    monkeypatch.setattr(
        broker_module.aio_pika,
        "connect_robust",
        connect_mock,
    )

    rabbitmq_broker = _build_broker(
        api_settings
    )

    await rabbitmq_broker.connect()

    connect_mock.assert_awaited_once_with(
        api_settings.rabbitmq_url
    )

    assert fake_connection.channel_calls == [
        True,
    ]

    channel = fake_connection.channel_object

    assert channel.declare_queue_calls == [
        {
            "name": "test.jobs",
            "durable": True,
            "arguments": JOB_QUEUE_ARGUMENTS,
        },
        {
            "name": "test.events",
            "durable": True,
            "arguments": None,
        },
    ]

    assert rabbitmq_broker.job_queue is (
        channel.queues["test.jobs"]
    )

    assert rabbitmq_broker.event_queue is (
        channel.queues["test.events"]
    )

    event_queue = channel.queues[
        "test.events"
    ]

    assert len(
        event_queue.consume_calls
    ) == 1

    assert (
        event_queue
        .consume_calls[0]["no_ack"]
        is False
    )

    assert rabbitmq_broker.is_connected()


# Vérifie qu'un second appel à connect()
# ne crée pas une nouvelle connexion.
@pytest.mark.anyio
async def test_connect_does_not_reconnect_when_already_connected(
    api_settings: ApiSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_mock = AsyncMock()

    monkeypatch.setattr(
        broker_module.aio_pika,
        "connect_robust",
        connect_mock,
    )

    rabbitmq_broker = _build_broker(
        api_settings
    )

    rabbitmq_broker.connection = (
        FakeConnection()
    )

    rabbitmq_broker.channel = (
        FakeChannel()
    )

    await rabbitmq_broker.connect()

    connect_mock.assert_not_awaited()


# Vérifie que close() ferme RabbitMQ
# puis supprime toutes les anciennes références.
@pytest.mark.anyio
async def test_close_clears_all_references(
    api_settings: ApiSettings,
) -> None:
    rabbitmq_broker = _build_broker(
        api_settings
    )

    connection = FakeConnection()

    rabbitmq_broker.connection = connection
    rabbitmq_broker.channel = FakeChannel()
    rabbitmq_broker.job_queue = FakeQueue(
        "test.jobs"
    )
    rabbitmq_broker.event_queue = FakeQueue(
        "test.events"
    )

    await rabbitmq_broker.close()

    assert connection.close_count == 1
    assert connection.is_closed is True

    assert rabbitmq_broker.connection is None
    assert rabbitmq_broker.channel is None
    assert rabbitmq_broker.job_queue is None
    assert rabbitmq_broker.event_queue is None

    assert not rabbitmq_broker.is_connected()