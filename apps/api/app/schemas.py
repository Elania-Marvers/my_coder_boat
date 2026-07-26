"""
FICHIER :
apps/api/app/schemas.py

RÔLE GÉNÉRAL :
Définit et valide tous les contrats de données
utilisés par l'API FastAPI.

Ces modèles décrivent :

1. les messages reçus depuis Django ;
2. les tickets publiés dans RabbitMQ ;
3. les événements publiés par le worker ;
4. les états retournés au front.

CIRCULATION ALLER :
apps/front/chat/services.py::create_job()
→ CreateJobRequest
→ WorkerJobMessage
→ RabbitMQ mycoder.jobs
→ RabbitMQWorker.run()

CIRCULATION RETOUR :
RabbitMQWorker.publish_event()
→ WorkerEventMessage
→ RabbitMQ mycoder.events
→ RabbitMQBroker._handle_event()
→ JobStore.apply_event()
→ JobStatusResponse
→ Django

APPELÉ PAR :
- apps/api/app/main.py
- apps/api/app/broker.py
- apps/api/app/job_store.py

PIPELINES :
- CHAT_JOB_CREATE
- CHAT_JOB_PUBLISH
- CHAT_JOB_EVENT
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


# Nombre maximal de messages transmis
# dans une conversation.
MAX_HISTORY_MESSAGES: Final[int] = 20


# Taille maximale d'un message utilisateur
# ou assistant transmis au modèle.
MAX_MESSAGE_LENGTH: Final[int] = 32_000


# Taille maximale d'une réponse complète
# renvoyée par le worker.
MAX_RESULT_LENGTH: Final[int] = 128_000


# Taille maximale du nom d'un modèle Ollama.
MAX_MODEL_NAME_LENGTH: Final[int] = 500


# Taille maximale d'un message d'erreur
# transmis entre le worker et le front.
MAX_ERROR_LENGTH: Final[int] = 4_000


# Type partagé par les messages de conversation
# acceptés depuis le front Django.
ChatRole = Literal[
    "user",
    "assistant",
]


# RÔLE :
# Liste les différents états possibles d'un ticket.
#
# UTILISÉE PAR :
# - WorkerEventMessage
# - JobStatusResponse
# - apps/api/app/job_store.py
#
# TRANSITIONS NORMALES :
# queued → running → completed
# queued → failed
# running → failed
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# RÔLE :
# Représente un message de conversation
# reçu par FastAPI depuis Django.
#
# CONSTRUIT PAR :
# - apps/front/chat/services.py::create_job()
#
# VALIDÉ PAR :
# - CreateJobRequest
#
# IMPORTANT :
# Le rôle system est volontairement interdit.
# Le prompt système est contrôlé uniquement par :
#
# apps/worker/src/local_qwen_worker/prompts.py
#
# PIPELINE :
# - CHAT_JOB_CREATE
class ChatMessageInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    role: ChatRole

    content: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH,
    )


# RÔLE :
# Représente le corps JSON reçu par la route
# POST /v1/jobs.
#
# CONSTRUIT PAR :
# - apps/front/chat/services.py::create_job()
#
# CONSOMMÉ PAR :
# - apps/api/app/main.py::create_job()
#
# APPELLE ENSUITE :
# - WorkerJobMessage
# - RabbitMQBroker.publish_job()
#
# CONTRAT :
# La conversation doit se terminer par
# un message utilisateur.
#
# PIPELINE :
# - CHAT_JOB_CREATE
class CreateJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    messages: list[ChatMessageInput] = Field(
        min_length=1,
        max_length=MAX_HISTORY_MESSAGES,
    )

    # RÔLE :
    # Vérifie que la dernière entrée de la conversation
    # correspond bien à une nouvelle question utilisateur.
    #
    # APPELÉE PAR :
    # - Pydantic pendant la validation de POST /v1/jobs
    #
    # ERREUR HTTP INDIRECTE :
    # FastAPI retourne HTTP 422 lorsque cette validation échoue.
    #
    # PIPELINE :
    # - CHAT_JOB_CREATE
    @model_validator(mode="after")
    def validate_last_message_is_user(
        self,
    ) -> Self:
        if self.messages[-1].role != "user":
            raise ValueError(
                "La conversation doit se terminer "
                "par un message utilisateur."
            )

        return self


# RÔLE :
# Représente le ticket publié par FastAPI
# dans la file RabbitMQ mycoder.jobs.
#
# CONSTRUIT PAR :
# - apps/api/app/main.py
#   ::_build_worker_job_message()
#
# PUBLIÉ PAR :
# - apps/api/app/broker.py
#   ::RabbitMQBroker.publish_job()
#
# CONSOMMÉ PAR :
# - apps/worker/src/local_qwen_worker/
#   rabbitmq_worker.py::RabbitMQWorker.run()
#
# PIPELINE :
# - CHAT_JOB_PUBLISH
class WorkerJobMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    job_id: UUID

    messages: list[ChatMessageInput] = Field(
        min_length=1,
        max_length=MAX_HISTORY_MESSAGES,
    )

    # RÔLE :
    # Vérifie une seconde fois que le ticket RabbitMQ
    # se termine par une question utilisateur.
    #
    # Cette validation protège le worker même lorsqu'un
    # WorkerJobMessage est construit ailleurs que depuis
    # CreateJobRequest.
    #
    # PIPELINE :
    # - CHAT_JOB_PUBLISH
    @model_validator(mode="after")
    def validate_last_message_is_user(
        self,
    ) -> Self:
        if self.messages[-1].role != "user":
            raise ValueError(
                "Le ticket RabbitMQ doit se terminer "
                "par un message utilisateur."
            )

        return self


# RÔLE :
# Représente un événement publié par le worker
# dans la file RabbitMQ mycoder.events.
#
# CONSTRUIT PAR :
# - apps/worker/src/local_qwen_worker/
#   rabbitmq_worker.py::RabbitMQWorker.process_job()
#
# PUBLIÉ PAR :
# - RabbitMQWorker.publish_event()
#
# CONSOMMÉ PAR :
# - apps/api/app/broker.py
#   ::RabbitMQBroker._handle_event()
#
# APPLIQUÉ PAR :
# - apps/api/app/job_store.py
#   ::JobStore.apply_event()
#
# PIPELINES :
# - CHAT_JOB_EVENT
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class WorkerEventMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    job_id: UUID
    state: JobState
    occurred_at: datetime

    # None signifie que la progression exacte
    # n'est pas calculable.
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
    # Vérifie que les données de l'événement
    # correspondent réellement à son état.
    #
    # APPELÉE PAR :
    # - Pydantic dans RabbitMQBroker._parse_event()
    #
    # RÈGLES :
    #
    # queued :
    # - progression forcée à 0 ;
    # - aucun résultat ;
    # - aucune erreur.
    #
    # running :
    # - progression facultative entre 0 et 99 ;
    # - aucun résultat ;
    # - aucune erreur.
    #
    # completed :
    # - contenu obligatoire ;
    # - modèle obligatoire ;
    # - progression forcée à 100 ;
    # - aucune erreur.
    #
    # failed :
    # - erreur obligatoire ;
    # - aucun résultat ;
    # - aucune progression.
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
                "La date de l'événement doit contenir "
                "un fuseau horaire."
            )

        if self.state == JobState.QUEUED:
            self._validate_queued_event()
            return self

        if self.state == JobState.RUNNING:
            self._validate_running_event()
            return self

        if self.state == JobState.COMPLETED:
            self._validate_completed_event()
            return self

        if self.state == JobState.FAILED:
            self._validate_failed_event()

        return self

    # RÔLE :
    # Valide les champs d'un événement queued.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _validate_queued_event(
        self,
    ) -> None:
        if (
            self.content is not None
            or self.model is not None
            or self.error is not None
        ):
            raise ValueError(
                "Un événement queued ne peut contenir "
                "ni résultat ni erreur."
            )

        if self.progress_percent not in {
            None,
            0,
        }:
            raise ValueError(
                "Un événement queued doit avoir "
                "une progression de 0 %."
            )

        self.progress_percent = 0

    # RÔLE :
    # Valide les champs d'un événement running.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _validate_running_event(
        self,
    ) -> None:
        if (
            self.content is not None
            or self.model is not None
            or self.error is not None
        ):
            raise ValueError(
                "Un événement running ne peut contenir "
                "ni résultat ni erreur."
            )

        if (
            self.progress_percent is not None
            and self.progress_percent >= 100
        ):
            raise ValueError(
                "Un job running ne peut pas être "
                "annoncé à 100 %."
            )

    # RÔLE :
    # Valide les champs d'un événement completed.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_COMPLETE
    def _validate_completed_event(
        self,
    ) -> None:
        if not self.content:
            raise ValueError(
                "Un événement completed doit contenir "
                "la réponse du modèle."
            )

        if not self.model:
            raise ValueError(
                "Un événement completed doit contenir "
                "le nom du modèle."
            )

        if self.error is not None:
            raise ValueError(
                "Un événement completed ne peut pas "
                "contenir d'erreur."
            )

        if self.progress_percent not in {
            None,
            100,
        }:
            raise ValueError(
                "Un événement completed doit avoir "
                "une progression de 100 %."
            )

        self.progress_percent = 100

    # RÔLE :
    # Valide les champs d'un événement failed.
    #
    # APPELÉE PAR :
    # - validate_payload_for_state()
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    def _validate_failed_event(
        self,
    ) -> None:
        if not self.error:
            raise ValueError(
                "Un événement failed doit contenir "
                "un message d'erreur."
            )

        if (
            self.content is not None
            or self.model is not None
        ):
            raise ValueError(
                "Un événement failed ne peut pas "
                "contenir de résultat."
            )

        if self.progress_percent is not None:
            raise ValueError(
                "Un événement failed ne doit pas "
                "contenir de progression."
            )


# RÔLE :
# Représente la réponse finale produite
# par le modèle Qwen.
#
# CONSTRUIT PAR :
# - apps/api/app/job_store.py
#   ::JobStore._apply_completed_event_locked()
#
# INCLUS DANS :
# - JobStatusResponse.result
#
# CONSOMMÉ PAR :
# - apps/front/chat/services.py
#   ::_parse_optional_result()
#
# PIPELINE :
# - CHAT_JOB_COMPLETE
class JobResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    content: str = Field(
        min_length=1,
        max_length=MAX_RESULT_LENGTH,
    )

    model: str = Field(
        min_length=1,
        max_length=MAX_MODEL_NAME_LENGTH,
    )


# RÔLE :
# Représente l'état public retourné à Django
# par les routes FastAPI.
#
# CONSTRUIT PAR :
# - apps/api/app/job_store.py
#   ::JobStore._build_response_locked()
#
# RETOURNÉ PAR :
# - POST /v1/jobs
# - GET /v1/jobs/{job_id}
#
# CONSOMMÉ PAR :
# - apps/front/chat/services.py::create_job()
# - apps/front/chat/services.py::get_job_status()
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class JobStatusResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    job_id: UUID
    state: JobState

    # Position du ticket parmi les jobs queued.
    queue_position: int | None = Field(
        default=None,
        ge=1,
    )

    # Nombre total de tickets actuellement queued.
    queue_total: int = Field(
        default=0,
        ge=0,
    )

    progress_percent: int | None = Field(
        default=None,
        ge=0,
        le=100,
    )

    result: JobResult | None = None

    error: str | None = Field(
        default=None,
        max_length=MAX_ERROR_LENGTH,
    )

    created_at: datetime
    updated_at: datetime

    # RÔLE :
    # Vérifie la cohérence finale de l'état
    # exposé par FastAPI au front Django.
    #
    # APPELÉE PAR :
    # - Pydantic lors de la construction
    #   de JobStatusResponse dans JobStore
    #
    # PIPELINES :
    # - CHAT_JOB_STATUS
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    @model_validator(mode="after")
    def validate_public_status(
        self,
    ) -> Self:
        self._validate_dates()
        self._validate_queue_information()

        if self.state == JobState.QUEUED:
            self._validate_queued_status()

        elif self.state == JobState.RUNNING:
            self._validate_running_status()

        elif self.state == JobState.COMPLETED:
            self._validate_completed_status()

        elif self.state == JobState.FAILED:
            self._validate_failed_status()

        return self

    # RÔLE :
    # Vérifie que les dates sont ordonnées
    # et contiennent un fuseau horaire.
    #
    # APPELÉE PAR :
    # - validate_public_status()
    def _validate_dates(
        self,
    ) -> None:
        if (
            self.created_at.tzinfo is None
            or self.updated_at.tzinfo is None
        ):
            raise ValueError(
                "Les dates du job doivent contenir "
                "un fuseau horaire."
            )

        if self.updated_at < self.created_at:
            raise ValueError(
                "updated_at ne peut pas être antérieur "
                "à created_at."
            )

    # RÔLE :
    # Vérifie que la position annoncée ne dépasse
    # pas le nombre total de tickets queued.
    #
    # APPELÉE PAR :
    # - validate_public_status()
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _validate_queue_information(
        self,
    ) -> None:
        if (
            self.queue_position is not None
            and self.queue_position > self.queue_total
        ):
            raise ValueError(
                "La position du job ne peut pas dépasser "
                "le nombre total de jobs en attente."
            )

    # RÔLE :
    # Valide une réponse publique queued.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _validate_queued_status(
        self,
    ) -> None:
        if self.result is not None:
            raise ValueError(
                "Un job queued ne peut pas "
                "contenir de résultat."
            )

        if self.error is not None:
            raise ValueError(
                "Un job queued ne peut pas "
                "contenir d'erreur."
            )

        if self.progress_percent != 0:
            raise ValueError(
                "Un job queued doit avoir "
                "une progression de 0 %."
            )

    # RÔLE :
    # Valide une réponse publique running.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _validate_running_status(
        self,
    ) -> None:
        if self.queue_position is not None:
            raise ValueError(
                "Un job running ne doit plus avoir "
                "de position dans la file."
            )

        if self.result is not None:
            raise ValueError(
                "Un job running ne peut pas "
                "contenir de résultat."
            )

        if self.error is not None:
            raise ValueError(
                "Un job running ne peut pas "
                "contenir d'erreur."
            )

        if (
            self.progress_percent is not None
            and self.progress_percent >= 100
        ):
            raise ValueError(
                "Un job running ne peut pas "
                "être annoncé à 100 %."
            )

    # RÔLE :
    # Valide une réponse publique completed.
    #
    # PIPELINE :
    # - CHAT_JOB_COMPLETE
    def _validate_completed_status(
        self,
    ) -> None:
        if self.queue_position is not None:
            raise ValueError(
                "Un job completed ne doit plus avoir "
                "de position dans la file."
            )

        if self.result is None:
            raise ValueError(
                "Un job completed doit contenir "
                "un résultat."
            )

        if self.error is not None:
            raise ValueError(
                "Un job completed ne peut pas "
                "contenir d'erreur."
            )

        if self.progress_percent != 100:
            raise ValueError(
                "Un job completed doit avoir "
                "une progression de 100 %."
            )

    # RÔLE :
    # Valide une réponse publique failed.
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    def _validate_failed_status(
        self,
    ) -> None:
        if self.queue_position is not None:
            raise ValueError(
                "Un job failed ne doit plus avoir "
                "de position dans la file."
            )

        if self.result is not None:
            raise ValueError(
                "Un job failed ne peut pas "
                "contenir de résultat."
            )

        if not self.error:
            raise ValueError(
                "Un job failed doit contenir "
                "un message d'erreur."
            )

        if self.progress_percent is not None:
            raise ValueError(
                "Un job failed ne doit pas "
                "contenir de progression."
            )