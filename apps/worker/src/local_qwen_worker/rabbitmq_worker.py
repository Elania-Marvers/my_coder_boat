"""
FICHIER :
apps/worker/src/local_qwen_worker/rabbitmq_worker.py

RÔLE GÉNÉRAL :
Exécute le processus worker chargé de consommer les tickets
présents dans la file RabbitMQ mycoder.jobs.

Pour chaque ticket, le worker :

1. valide le message RabbitMQ ;
2. publie un événement running ;
3. transmet la conversation à QwenService ;
4. attend la réponse d'Ollama ;
5. publie completed ou failed ;
6. acquitte le ticket RabbitMQ.

CIRCULATION ALLER :
FastAPI
→ RabbitMQ mycoder.jobs
→ RabbitMQWorker.run()
→ RabbitMQWorker.process_job()
→ QwenService.chat()
→ OllamaClient.chat()
→ Ollama

CIRCULATION RETOUR :
Ollama
→ ChatResult
→ RabbitMQWorker.publish_event()
→ RabbitMQ mycoder.events
→ FastAPI RabbitMQBroker._handle_event()
→ JobStore.apply_event()

POINT D'ENTRÉE :
- commande mycoder-worker ;
- fonction main() ;
- déclaration située dans apps/worker/pyproject.toml.

PIPELINES :
- STARTUP
- CHAT_JOB_CONSUME
- CHAT_JOB_GENERATE
- CHAT_JOB_EVENT
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import asyncio
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Self
from uuid import UUID

import aio_pika
from aio_pika import (
    DeliveryMode,
    IncomingMessage,
    Message,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from .config import (
    WorkerSettings,
    get_settings,
)
from .ollama_client import QwenWorkerError
from .prompts import DEFAULT_SYSTEM_PROMPT
from .schemas import (
    ChatMessage,
    ChatResult,
)
from .service import QwenService


# Logger principal du processus worker.
logger = logging.getLogger(__name__)


# Nombre maximal de messages accepté
# dans un ticket provenant de FastAPI.
MAX_HISTORY_MESSAGES: Final[int] = 20


# Taille maximale d'une réponse publiée vers FastAPI.
#
# Cette valeur doit rester cohérente avec :
# apps/api/app/schemas.py::MAX_RESULT_LENGTH
MAX_RESULT_LENGTH: Final[int] = 128_000


# Taille maximale du nom du modèle.
#
# Cette valeur doit rester cohérente avec :
# apps/api/app/schemas.py::MAX_MODEL_NAME_LENGTH
MAX_MODEL_NAME_LENGTH: Final[int] = 500


# Taille maximale d'une erreur transmise au front.
#
# Cette valeur doit rester cohérente avec :
# apps/api/app/schemas.py::MAX_ERROR_LENGTH
MAX_ERROR_LENGTH: Final[int] = 4_000


# Arguments utilisés lors de la déclaration
# de la file RabbitMQ mycoder.jobs.
#
# Cette configuration doit rester identique à celle utilisée par :
# apps/api/app/broker.py::JOB_QUEUE_ARGUMENTS
JOB_QUEUE_ARGUMENTS: Final[dict[str, bool]] = {
    "x-single-active-consumer": True,
}


# RÔLE :
# Liste les états que le worker peut publier.
#
# IMPORTANT :
# Le worker ne publie pas queued.
# Cet état est créé directement par FastAPI
# avant la publication du ticket.
#
# UTILISÉE PAR :
# - WorkerEventMessage
# - RabbitMQWorker._publish_running()
# - RabbitMQWorker._publish_completed()
# - RabbitMQWorker._publish_failed()
#
# PIPELINES :
# - CHAT_JOB_EVENT
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class WorkerEventState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# RÔLE :
# Représente un ticket reçu depuis
# la file RabbitMQ mycoder.jobs.
#
# PRODUIT PAR :
# - apps/api/app/main.py::_build_worker_job_message()
# - apps/api/app/broker.py::RabbitMQBroker.publish_job()
#
# VALIDÉ PAR :
# - RabbitMQWorker._parse_job_message()
#
# CONSOMMÉ PAR :
# - RabbitMQWorker.process_job()
#
# PIPELINE :
# - CHAT_JOB_CONSUME
class WorkerJobMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    job_id: UUID

    messages: list[ChatMessage] = Field(
        min_length=1,
        max_length=MAX_HISTORY_MESSAGES,
    )

    # RÔLE :
    # Vérifie que FastAPI n'a pas envoyé
    # de rôle système dans la conversation.
    #
    # Le prompt système est contrôlé localement par :
    # apps/worker/src/local_qwen_worker/prompts.py
    #
    # Vérifie également que la conversation
    # se termine par un message utilisateur.
    #
    # APPELÉE PAR :
    # - Pydantic pendant _parse_job_message()
    #
    # PIPELINE :
    # - CHAT_JOB_CONSUME
    @model_validator(mode="after")
    def validate_conversation(
        self,
    ) -> Self:
        if any(
            message.role == "system"
            for message in self.messages
        ):
            raise ValueError(
                "Un ticket RabbitMQ ne peut pas "
                "fournir de message système."
            )

        if self.messages[-1].role != "user":
            raise ValueError(
                "La conversation du ticket doit "
                "se terminer par un message utilisateur."
            )

        return self


# RÔLE :
# Représente un événement publié par le worker
# dans la file RabbitMQ mycoder.events.
#
# PRODUIT PAR :
# - RabbitMQWorker._publish_running()
# - RabbitMQWorker._publish_completed()
# - RabbitMQWorker._publish_failed()
#
# PUBLIÉ PAR :
# - RabbitMQWorker.publish_event()
#
# CONSOMMÉ PAR :
# - apps/api/app/broker.py
#   ::RabbitMQBroker._handle_event()
#
# PIPELINES :
# - CHAT_JOB_EVENT
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class WorkerEventMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    job_id: UUID
    state: WorkerEventState
    occurred_at: datetime

    progress_percent: int | None = Field(
        default=None,
        ge=0,
        le=100,
    )

    content: str | None = Field(
        default=None,
        max_length=MAX_RESULT_LENGTH,
    )

    model: str | None = Field(
        default=None,
        max_length=MAX_MODEL_NAME_LENGTH,
    )

    error: str | None = Field(
        default=None,
        max_length=MAX_ERROR_LENGTH,
    )

    # RÔLE :
    # Vérifie la cohérence entre l'état
    # et les informations publiées.
    #
    # RÈGLES :
    # - running ne contient aucun résultat ;
    # - completed contient une réponse et un modèle ;
    # - failed contient obligatoirement une erreur ;
    # - les dates doivent contenir un fuseau horaire.
    #
    # APPELÉE PAR :
    # - Pydantic avant chaque publication RabbitMQ.
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    @model_validator(mode="after")
    def validate_payload_for_state(
        self,
    ) -> Self:
        if self.occurred_at.tzinfo is None:
            raise ValueError(
                "La date de l'événement doit "
                "contenir un fuseau horaire."
            )

        if self.state == WorkerEventState.RUNNING:
            self._validate_running()
            return self

        if self.state == WorkerEventState.COMPLETED:
            self._validate_completed()
            return self

        if self.state == WorkerEventState.FAILED:
            self._validate_failed()

        return self

    # RÔLE :
    # Valide un événement running.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_EVENT
    def _validate_running(
        self,
    ) -> None:
        if (
            self.content is not None
            or self.model is not None
            or self.error is not None
        ):
            raise ValueError(
                "Un événement running ne peut "
                "contenir ni résultat ni erreur."
            )

        if (
            self.progress_percent is not None
            and self.progress_percent >= 100
        ):
            raise ValueError(
                "Un événement running ne peut "
                "pas annoncer 100 %."
            )

    # RÔLE :
    # Valide un événement completed.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_COMPLETE
    def _validate_completed(
        self,
    ) -> None:
        if not self.content:
            raise ValueError(
                "Un événement completed doit "
                "contenir la réponse du modèle."
            )

        if not self.model:
            raise ValueError(
                "Un événement completed doit "
                "contenir le nom du modèle."
            )

        if self.error is not None:
            raise ValueError(
                "Un événement completed ne peut "
                "pas contenir d'erreur."
            )

        if self.progress_percent not in {
            None,
            100,
        }:
            raise ValueError(
                "Un événement completed doit "
                "avoir une progression de 100 %."
            )

        self.progress_percent = 100

    # RÔLE :
    # Valide un événement failed.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    def _validate_failed(
        self,
    ) -> None:
        if not self.error:
            raise ValueError(
                "Un événement failed doit "
                "contenir une erreur."
            )

        if (
            self.content is not None
            or self.model is not None
        ):
            raise ValueError(
                "Un événement failed ne peut "
                "pas contenir de résultat."
            )

        if self.progress_percent is not None:
            raise ValueError(
                "Un événement failed ne doit "
                "pas contenir de progression."
            )


# RÔLE :
# Consomme les tickets RabbitMQ
# et exécute Qwen une demande à la fois.
#
# INSTANCIÉE PAR :
# - async_main()
#
# APPELLE :
# - RabbitMQ ;
# - QwenService ;
# - OllamaClient indirectement.
#
# PIPELINES :
# - STARTUP
# - CHAT_JOB_CONSUME
# - CHAT_JOB_GENERATE
# - CHAT_JOB_EVENT
class RabbitMQWorker:
    # RÔLE :
    # Initialise la configuration, QwenService
    # et les références RabbitMQ.
    #
    # APPELÉE PAR :
    # - async_main()
    #
    # APPELLE :
    # - get_settings()
    # - QwenService()
    #
    # PIPELINE :
    # - STARTUP
    def __init__(
        self,
        settings: WorkerSettings | None = None,
    ) -> None:
        self.settings = (
            settings
            or get_settings()
        )

        self.qwen_service = QwenService()

        self.connection: (
            aio_pika.abc.AbstractRobustConnection
            | None
        ) = None

        self.channel: (
            aio_pika.abc.AbstractRobustChannel
            | None
        ) = None

        self.job_queue: (
            aio_pika.abc.AbstractQueue
            | None
        ) = None

        self.event_queue: (
            aio_pika.abc.AbstractQueue
            | None
        ) = None

    # RÔLE :
    # Ouvre RabbitMQ, crée le canal,
    # configure le traitement séquentiel
    # puis déclare les deux files.
    #
    # APPELÉE PAR :
    # - async_main()
    #
    # APPELLE :
    # - aio_pika.connect_robust()
    # - channel.set_qos()
    # - _declare_job_queue()
    # - _declare_event_queue()
    #
    # CONFIGURATION :
    # prefetch_count=1 garantit qu'un worker
    # ne reçoit pas un second ticket avant
    # l'acquittement du précédent.
    #
    # PIPELINE :
    # - STARTUP
    async def connect(self) -> None:
        if self.is_connected():
            logger.warning(
                "Le worker est déjà connecté à RabbitMQ."
            )
            return

        logger.info(
            "Connexion du worker à RabbitMQ."
        )

        self.connection = (
            await aio_pika.connect_robust(
                self.settings.rabbitmq_url
            )
        )

        self.channel = (
            await self.connection.channel(
                publisher_confirms=True
            )
        )

        await self.channel.set_qos(
            prefetch_count=1
        )

        self.job_queue = (
            await self._declare_job_queue()
        )

        self.event_queue = (
            await self._declare_event_queue()
        )

        logger.info(
            "Worker connecté à RabbitMQ."
        )

    # RÔLE :
    # Ferme la connexion RabbitMQ du worker.
    #
    # APPELÉE PAR :
    # - async_main()
    #
    # MODIFIE :
    # - self.connection
    # - self.channel
    # - self.job_queue
    # - self.event_queue
    #
    # PIPELINE :
    # - STARTUP
    async def close(self) -> None:
        if (
            self.connection is not None
            and not self.connection.is_closed
        ):
            logger.info(
                "Fermeture de RabbitMQ côté worker."
            )

            await self.connection.close()

        self.connection = None
        self.channel = None
        self.job_queue = None
        self.event_queue = None

    # RÔLE :
    # Indique si la connexion et le canal
    # RabbitMQ sont ouverts.
    #
    # APPELÉE PAR :
    # - connect()
    #
    # RETOURNE :
    # - True si RabbitMQ est disponible ;
    # - False sinon.
    #
    # PIPELINE :
    # - STARTUP
    def is_connected(self) -> bool:
        return bool(
            self.connection is not None
            and not self.connection.is_closed
            and self.channel is not None
            and not self.channel.is_closed
        )

    # RÔLE :
    # Retourne le canal RabbitMQ ouvert.
    #
    # APPELÉE PAR :
    # - _declare_job_queue()
    # - _declare_event_queue()
    # - publish_event()
    #
    # ERREUR :
    # - RuntimeError si connect() n'a pas terminé.
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_EVENT
    def _get_channel_or_raise(
        self,
    ) -> aio_pika.abc.AbstractRobustChannel:
        if (
            self.channel is None
            or self.channel.is_closed
        ):
            raise RuntimeError(
                "Le canal RabbitMQ du worker "
                "n'est pas disponible."
            )

        return self.channel

    # RÔLE :
    # Déclare la file mycoder.jobs
    # avec les mêmes paramètres que FastAPI.
    #
    # APPELÉE PAR :
    # - connect()
    #
    # RETOURNE :
    # - La file consommée par run().
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_CONSUME
    async def _declare_job_queue(
        self,
    ) -> aio_pika.abc.AbstractQueue:
        channel = self._get_channel_or_raise()

        return await channel.declare_queue(
            self.settings.rabbitmq_job_queue,
            durable=True,
            arguments=JOB_QUEUE_ARGUMENTS,
        )

    # RÔLE :
    # Déclare la file mycoder.events
    # utilisée pour publier les états.
    #
    # APPELÉE PAR :
    # - connect()
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_EVENT
    async def _declare_event_queue(
        self,
    ) -> aio_pika.abc.AbstractQueue:
        channel = self._get_channel_or_raise()

        return await channel.declare_queue(
            self.settings.rabbitmq_event_queue,
            durable=True,
        )

    # RÔLE :
    # Transforme un événement Pydantic
    # en message AMQP persistant.
    #
    # APPELÉE PAR :
    # - publish_event()
    #
    # RETOURNE :
    # - aio_pika.Message
    #
    # PIPELINE :
    # - CHAT_JOB_EVENT
    @staticmethod
    def _build_event_message(
        event: WorkerEventMessage,
    ) -> Message:
        return Message(
            body=event.model_dump_json().encode(
                "utf-8"
            ),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=str(event.job_id),
            correlation_id=str(event.job_id),
            timestamp=event.occurred_at,
        )

    # RÔLE :
    # Publie un événement dans mycoder.events.
    #
    # APPELÉE PAR :
    # - _publish_running()
    # - _publish_completed()
    # - _publish_failed()
    #
    # APPELLE :
    # - _get_channel_or_raise()
    # - _build_event_message()
    # - RabbitMQ default exchange.publish()
    #
    # CONSOMMÉ PAR :
    # - apps/api/app/broker.py
    #   ::RabbitMQBroker._handle_event()
    #
    # PIPELINE :
    # - CHAT_JOB_EVENT
    async def publish_event(
        self,
        event: WorkerEventMessage,
    ) -> None:
        channel = self._get_channel_or_raise()

        message = self._build_event_message(
            event
        )

        await channel.default_exchange.publish(
            message,
            routing_key=(
                self.settings.rabbitmq_event_queue
            ),
        )

        logger.info(
            "Événement %s publié pour le job %s.",
            event.state,
            event.job_id,
        )

    # RÔLE :
    # Publie l'état running avant
    # le début de la génération Qwen.
    #
    # APPELÉE PAR :
    # - process_job()
    #
    # APPELLE :
    # - publish_event()
    #
    # PIPELINE :
    # - CHAT_JOB_EVENT
    async def _publish_running(
        self,
        job_id: UUID,
    ) -> None:
        await self.publish_event(
            WorkerEventMessage(
                job_id=job_id,
                state=WorkerEventState.RUNNING,
                progress_percent=None,
                occurred_at=datetime.now(UTC),
            )
        )

    # RÔLE :
    # Publie le résultat final produit par Qwen.
    #
    # APPELÉE PAR :
    # - process_job()
    #
    # APPELLE :
    # - publish_event()
    #
    # PIPELINE :
    # - CHAT_JOB_COMPLETE
    async def _publish_completed(
        self,
        job_id: UUID,
        result: ChatResult,
    ) -> None:
        await self.publish_event(
            WorkerEventMessage(
                job_id=job_id,
                state=WorkerEventState.COMPLETED,
                progress_percent=100,
                content=result.content,
                model=result.model,
                occurred_at=datetime.now(UTC),
            )
        )

    # RÔLE :
    # Publie un échec contrôlé vers FastAPI.
    #
    # APPELÉE PAR :
    # - process_job()
    #
    # APPELLE :
    # - _normalise_error_message()
    # - publish_event()
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    async def _publish_failed(
        self,
        job_id: UUID,
        error: str,
    ) -> None:
        await self.publish_event(
            WorkerEventMessage(
                job_id=job_id,
                state=WorkerEventState.FAILED,
                error=self._normalise_error_message(
                    error
                ),
                occurred_at=datetime.now(UTC),
            )
        )

    # RÔLE :
    # Nettoie et limite une erreur avant
    # sa publication dans RabbitMQ.
    #
    # APPELÉE PAR :
    # - _publish_failed()
    #
    # RETOURNE :
    # - Un message non vide de 4 000 caractères maximum.
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _normalise_error_message(
        error: str,
    ) -> str:
        clean_error = error.strip()

        if not clean_error:
            clean_error = (
                "Le worker a rencontré une erreur "
                "sans fournir de détail."
            )

        return clean_error[:MAX_ERROR_LENGTH]

    # RÔLE :
    # Appelle QwenService dans un fil d'exécution
    # afin de ne pas bloquer la boucle asyncio RabbitMQ.
    #
    # APPELÉE PAR :
    # - process_job()
    #
    # APPELLE :
    # - apps/worker/src/local_qwen_worker/
    #   service.py::QwenService.chat()
    # - apps/worker/src/local_qwen_worker/
    #   ollama_client.py::OllamaClient.chat()
    #
    # RETOURNE :
    # - ChatResult
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    async def _generate_response(
        self,
        job: WorkerJobMessage,
    ) -> ChatResult:
        return await asyncio.to_thread(
            self.qwen_service.chat,
            job.messages,
            DEFAULT_SYSTEM_PROMPT,
        )

    # RÔLE :
    # Vérifie que le résultat peut être publié
    # sans violer le contrat FastAPI.
    #
    # APPELÉE PAR :
    # - process_job()
    #
    # RETOURNE :
    # - None lorsque le résultat est valide ;
    # - un message d'erreur sinon.
    #
    # PIPELINES :
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _validate_result(
        result: ChatResult,
    ) -> str | None:
        if not result.content.strip():
            return (
                "Ollama a retourné une réponse vide."
            )

        if len(result.content) > MAX_RESULT_LENGTH:
            return (
                "La réponse produite par Ollama dépasse "
                "la taille maximale autorisée."
            )

        if not result.model.strip():
            return (
                "Ollama n'a pas indiqué le nom "
                "du modèle utilisé."
            )

        if len(result.model) > MAX_MODEL_NAME_LENGTH:
            return (
                "Le nom du modèle retourné par Ollama "
                "dépasse la taille maximale autorisée."
            )

        return None

    # RÔLE :
    # Exécute complètement un ticket Qwen.
    #
    # APPELÉE PAR :
    # - _handle_job_message()
    #
    # APPELLE :
    # - _publish_running()
    # - _generate_response()
    # - _validate_result()
    # - _publish_completed()
    # - _publish_failed()
    #
    # COMPORTEMENT :
    # - erreur Qwen contrôlée :
    #   publication de failed ;
    #
    # - erreur inattendue :
    #   publication d'un message générique ;
    #
    # - réussite :
    #   publication de completed.
    #
    # PIPELINES :
    # - CHAT_JOB_GENERATE
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    async def process_job(
        self,
        job: WorkerJobMessage,
    ) -> None:
        logger.info(
            "Début du job %s.",
            job.job_id,
        )

        # Informe FastAPI que le ticket
        # a quitté la file d'attente.
        await self._publish_running(
            job.job_id
        )

        try:
            result = await self._generate_response(
                job
            )

        except QwenWorkerError as exc:
            logger.exception(
                "Échec contrôlé du job %s.",
                job.job_id,
            )

            await self._publish_failed(
                job_id=job.job_id,
                error=str(exc),
            )

            return

        except Exception:
            logger.exception(
                "Échec inattendu du job %s.",
                job.job_id,
            )

            await self._publish_failed(
                job_id=job.job_id,
                error=(
                    "Une erreur inattendue est survenue "
                    "pendant la génération Qwen."
                ),
            )

            return

        result_error = self._validate_result(
            result
        )

        if result_error is not None:
            logger.error(
                "Résultat invalide pour le job %s : %s",
                job.job_id,
                result_error,
            )

            await self._publish_failed(
                job_id=job.job_id,
                error=result_error,
            )

            return

        await self._publish_completed(
            job_id=job.job_id,
            result=result,
        )

        logger.info(
            "Job %s terminé.",
            job.job_id,
        )

    # RÔLE :
    # Valide le JSON brut d'un ticket RabbitMQ.
    #
    # APPELÉE PAR :
    # - _handle_job_message()
    #
    # APPELLE :
    # - WorkerJobMessage.model_validate_json()
    #
    # RETOURNE :
    # - WorkerJobMessage valide ;
    # - None pour un message inutilisable.
    #
    # PIPELINE :
    # - CHAT_JOB_CONSUME
    @staticmethod
    def _parse_job_message(
        message: IncomingMessage,
    ) -> WorkerJobMessage | None:
        try:
            return (
                WorkerJobMessage
                .model_validate_json(
                    message.body
                )
            )

        except ValidationError:
            logger.exception(
                "Ticket RabbitMQ invalide."
            )

            return None

    # RÔLE :
    # Traite un message brut livré par RabbitMQ.
    #
    # APPELÉE PAR :
    # - run()
    #
    # APPELLE :
    # - _parse_job_message()
    # - process_job()
    # - IncomingMessage.ack()
    # - IncomingMessage.reject()
    # - IncomingMessage.nack()
    #
    # COMPORTEMENT :
    # - message invalide :
    #   rejet définitif ;
    #
    # - erreur d'infrastructure :
    #   remise dans la file ;
    #
    # - traitement terminé ou failed publié :
    #   acquittement du ticket.
    #
    # PIPELINES :
    # - CHAT_JOB_CONSUME
    # - CHAT_JOB_GENERATE
    # - CHAT_JOB_FAILURE
    async def _handle_job_message(
        self,
        message: IncomingMessage,
    ) -> None:
        job = self._parse_job_message(
            message
        )

        if job is None:
            await message.reject(
                requeue=False
            )
            return

        try:
            await self.process_job(
                job
            )

        except Exception:
            # Cette branche concerne principalement
            # une panne RabbitMQ pendant la publication
            # d'un événement running/completed/failed.
            logger.exception(
                "Le traitement du job %s n'a pas "
                "pu être finalisé dans RabbitMQ.",
                job.job_id,
            )

            await message.nack(
                requeue=True
            )

            return

        await message.ack()

        logger.info(
            "Ticket RabbitMQ %s acquitté.",
            job.job_id,
        )

    # RÔLE :
    # Écoute continuellement la file mycoder.jobs.
    #
    # APPELÉE PAR :
    # - async_main()
    #
    # APPELLE :
    # - _handle_job_message()
    #
    # TRAITEMENT SÉQUENTIEL :
    # - prefetch_count=1 ;
    # - un seul itérateur ;
    # - un message acquitté avant le suivant.
    #
    # PIPELINE :
    # - CHAT_JOB_CONSUME
    async def run(self) -> None:
        if self.job_queue is None:
            raise RuntimeError(
                "La file RabbitMQ des jobs "
                "n'est pas initialisée."
            )

        logger.info(
            "Worker prêt : un ticket à la fois."
        )

        async with (
            self.job_queue.iterator()
        ) as iterator:
            async for message in iterator:
                await self._handle_job_message(
                    message
                )


# RÔLE :
# Configure les logs, crée le worker,
# ouvre RabbitMQ puis lance l'écoute.
#
# APPELÉE PAR :
# - main()
#
# APPELLE :
# - RabbitMQWorker()
# - RabbitMQWorker.connect()
# - RabbitMQWorker.run()
# - RabbitMQWorker.close()
#
# PIPELINE :
# - STARTUP
async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | %(levelname)s | "
            "%(name)s | %(message)s"
        ),
    )

    worker = RabbitMQWorker()

    await worker.connect()

    try:
        await worker.run()

    finally:
        await worker.close()


# RÔLE :
# Point d'entrée synchrone de la commande mycoder-worker.
#
# APPELÉE PAR :
# - Makefile::worker
# - Makefile::dev
# - apps/worker/pyproject.toml
#
# APPELLE :
# - asyncio.run()
# - async_main()
#
# PIPELINE :
# - STARTUP
def main() -> None:
    try:
        asyncio.run(
            async_main()
        )

    except KeyboardInterrupt:
        logger.info(
            "Arrêt demandé par l'utilisateur."
        )


# Permet également de lancer le worker avec :
#
# python -m local_qwen_worker.rabbitmq_worker
if __name__ == "__main__":
    main()