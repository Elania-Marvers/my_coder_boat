"""
FICHIER :
apps/api/app/job_store.py

RÔLE GÉNÉRAL :
Conserve en mémoire l'état des tickets connus
par l'instance FastAPI actuellement active.

Le JobStore reçoit les changements d'état publiés
par le worker dans RabbitMQ :

queued
→ running
→ completed

ou :

queued / running
→ failed

APPELÉ PAR :
- apps/api/app/main.py::create_job()
- apps/api/app/main.py::_publish_job_or_raise()
- apps/api/app/main.py::_get_job_or_raise()
- apps/api/app/broker.py::RabbitMQBroker._handle_event()

APPELLE :
- apps/api/app/schemas.py
- asyncio.Lock

IMPORTANT :
Ce stockage n'est pas persistant.

Un redémarrage de FastAPI efface les tickets connus.
Une future version pourra remplacer cette classe
par Redis, PostgreSQL ou SQLite.

PIPELINES :
- CHAT_JOB_CREATE
- CHAT_JOB_PUBLISH
- CHAT_JOB_EVENT
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from .schemas import (
    JobResult,
    JobState,
    JobStatusResponse,
    WorkerEventMessage,
)


# Logger du stockage des tickets.
#
# Il signale notamment :
# - les transitions invalides ;
# - les événements anciens ;
# - les résultats incomplets.
logger = logging.getLogger(__name__)


# États considérés comme définitifs.
#
# Lorsqu'un ticket atteint completed ou failed,
# il ne doit normalement plus revenir vers running.
TERMINAL_JOB_STATES: Final[frozenset[JobState]] = (
    frozenset(
        {
            JobState.COMPLETED,
            JobState.FAILED,
        }
    )
)


# Transitions autorisées entre les états.
#
# Les transitions vers le même état sont acceptées
# afin que la réception répétée d'un événement
# reste sans conséquence dangereuse.
ALLOWED_STATE_TRANSITIONS: Final[
    dict[JobState, frozenset[JobState]]
] = {
    JobState.QUEUED: frozenset(
        {
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.COMPLETED,
            JobState.FAILED,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.RUNNING,
            JobState.COMPLETED,
            JobState.FAILED,
        }
    ),
    JobState.COMPLETED: frozenset(
        {
            JobState.COMPLETED,
        }
    ),
    JobState.FAILED: frozenset(
        {
            JobState.FAILED,
        }
    ),
}


# RÔLE :
# Représente la version interne et modifiable
# d'un ticket stocké par FastAPI.
#
# CONSTRUIT PAR :
# - JobStore.create_job()
# - JobStore._create_record_from_event_locked()
#
# CONVERTI EN :
# - JobStatusResponse
# - par JobStore._build_response_locked()
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
@dataclass(slots=True)
class JobRecord:
    job_id: UUID
    state: JobState

    created_at: datetime
    updated_at: datetime

    progress_percent: int | None = None
    result: JobResult | None = None
    error: str | None = None


# RÔLE :
# Stocke et met à jour les tickets connus par FastAPI.
#
# INSTANCIÉE PAR :
# - apps/api/app/main.py
#
# UTILISÉE PAR :
# - apps/api/app/main.py
# - apps/api/app/broker.py
#
# PROTECTION :
# Toutes les lectures et écritures passent par
# un asyncio.Lock afin d'éviter les modifications
# concurrentes entre les routes HTTP et RabbitMQ.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_EVENT
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class JobStore:
    # RÔLE :
    # Initialise le dictionnaire des tickets
    # et le verrou protégeant son accès.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py
    #
    # PIPELINE :
    # - STARTUP
    def __init__(self) -> None:
        self._jobs: dict[UUID, JobRecord] = {}
        self._lock = asyncio.Lock()

    # RÔLE :
    # Enregistre un nouveau ticket dans l'état queued.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py::create_job()
    #
    # APPELLE :
    # - _build_response_locked()
    #
    # MODIFIE :
    # - self._jobs
    #
    # RETOURNE :
    # - JobStatusResponse initial ;
    # - progression à 0 % ;
    # - position calculée dans la file.
    #
    # ERREUR :
    # - ValueError si le même UUID existe déjà.
    #
    # PIPELINE :
    # - CHAT_JOB_CREATE
    async def create_job(
        self,
        job_id: UUID,
    ) -> JobStatusResponse:
        now = datetime.now(UTC)

        async with self._lock:
            if job_id in self._jobs:
                raise ValueError(
                    f"Le job {job_id} existe déjà."
                )

            record = JobRecord(
                job_id=job_id,
                state=JobState.QUEUED,
                progress_percent=0,
                created_at=now,
                updated_at=now,
            )

            self._jobs[job_id] = record

            logger.info(
                "Job %s enregistré dans l'état queued.",
                job_id,
            )

            return self._build_response_locked(
                record
            )

    # RÔLE :
    # Marque un ticket comme échoué lorsque FastAPI
    # n'a pas réussi à le publier dans RabbitMQ.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py::_publish_job_or_raise()
    #
    # MODIFIE :
    # - state → failed
    # - progress_percent → None
    # - error
    # - updated_at
    #
    # PIPELINES :
    # - CHAT_JOB_PUBLISH
    # - CHAT_JOB_FAILURE
    async def mark_publish_failed(
        self,
        job_id: UUID,
        error: str,
    ) -> None:
        async with self._lock:
            record = self._jobs.get(job_id)

            if record is None:
                logger.warning(
                    "Impossible de marquer le job %s "
                    "comme échoué : ticket inconnu.",
                    job_id,
                )
                return

            self._mark_record_failed_locked(
                record=record,
                error=error,
                occurred_at=datetime.now(UTC),
            )

            logger.error(
                "Publication RabbitMQ échouée "
                "pour le job %s.",
                job_id,
            )

    # RÔLE :
    # Applique un événement running, completed ou failed
    # reçu depuis le worker via RabbitMQ.
    #
    # APPELÉE PAR :
    # - apps/api/app/broker.py
    #   ::RabbitMQBroker._handle_event()
    #
    # APPELLE :
    # - _create_record_from_event_locked()
    # - _is_transition_allowed_locked()
    # - _apply_event_to_record_locked()
    #
    # MODIFIE :
    # - self._jobs
    # - l'état, la progression, le résultat
    #   ou l'erreur du ticket.
    #
    # COMPORTEMENT :
    # - recrée un ticket minimal après un redémarrage API ;
    # - ignore les événements plus anciens ;
    # - refuse les retours depuis un état terminal ;
    # - accepte sans danger les événements répétés.
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_STATUS
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    async def apply_event(
        self,
        event: WorkerEventMessage,
    ) -> None:
        async with self._lock:
            record = self._jobs.get(
                event.job_id
            )

            # Après un redémarrage de FastAPI,
            # RabbitMQ peut encore livrer un événement
            # pour un ticket absent de la mémoire.
            if record is None:
                record = (
                    self._create_record_from_event_locked(
                        event
                    )
                )

                self._jobs[event.job_id] = record

                logger.warning(
                    "Le job %s a été recréé "
                    "depuis un événement RabbitMQ.",
                    event.job_id,
                )

                return

            # Un événement plus ancien que l'état connu
            # ne doit pas écraser une donnée plus récente.
            if event.occurred_at < record.updated_at:
                logger.warning(
                    "Événement ancien ignoré pour le job %s : "
                    "%s reçu après %s.",
                    event.job_id,
                    event.state,
                    record.state,
                )
                return

            if not self._is_transition_allowed_locked(
                current_state=record.state,
                next_state=event.state,
            ):
                logger.warning(
                    "Transition de job refusée pour %s : "
                    "%s → %s.",
                    event.job_id,
                    record.state,
                    event.state,
                )
                return

            self._apply_event_to_record_locked(
                record=record,
                event=event,
            )

            logger.info(
                "État du job %s mis à jour : %s.",
                event.job_id,
                record.state,
            )

    # RÔLE :
    # Retourne l'état public actuel d'un ticket.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py::_get_job_or_raise()
    #
    # APPELLE :
    # - _build_response_locked()
    #
    # RETOURNE :
    # - JobStatusResponse ;
    # - None si le ticket est inconnu.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    async def get_job(
        self,
        job_id: UUID,
    ) -> JobStatusResponse | None:
        async with self._lock:
            record = self._jobs.get(job_id)

            if record is None:
                return None

            return self._build_response_locked(
                record
            )

    # RÔLE :
    # Vérifie si une transition d'état est autorisée.
    #
    # APPELÉE PAR :
    # - apply_event()
    #
    # RETOURNE :
    # - True si la transition est prévue ;
    # - False dans le cas contraire.
    #
    # IMPORTANT :
    # Cette fonction doit être appelée uniquement
    # pendant que self._lock est détenu.
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _is_transition_allowed_locked(
        current_state: JobState,
        next_state: JobState,
    ) -> bool:
        allowed_next_states = (
            ALLOWED_STATE_TRANSITIONS[
                current_state
            ]
        )

        return next_state in allowed_next_states

    # RÔLE :
    # Construit un ticket minimal à partir
    # d'un événement reçu après un redémarrage API.
    #
    # APPELÉE PAR :
    # - apply_event()
    #
    # APPELLE :
    # - _apply_event_to_record_locked()
    #
    # RETOURNE :
    # - JobRecord initialisé et mis à jour.
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    def _create_record_from_event_locked(
        self,
        event: WorkerEventMessage,
    ) -> JobRecord:
        record = JobRecord(
            job_id=event.job_id,
            state=event.state,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )

        self._apply_event_to_record_locked(
            record=record,
            event=event,
        )

        return record

    # RÔLE :
    # Applique les champs d'un événement validé
    # sur un JobRecord existant.
    #
    # APPELÉE PAR :
    # - apply_event()
    # - _create_record_from_event_locked()
    #
    # APPELLE :
    # - _apply_running_event_locked()
    # - _apply_completed_event_locked()
    # - _apply_failed_event_locked()
    #
    # MODIFIE :
    # - state
    # - progress_percent
    # - result
    # - error
    # - updated_at
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    def _apply_event_to_record_locked(
        self,
        record: JobRecord,
        event: WorkerEventMessage,
    ) -> None:
        record.state = event.state
        record.updated_at = event.occurred_at

        if event.state == JobState.QUEUED:
            record.progress_percent = 0
            record.result = None
            record.error = None
            return

        if event.state == JobState.RUNNING:
            self._apply_running_event_locked(
                record
            )
            return

        if event.state == JobState.COMPLETED:
            self._apply_completed_event_locked(
                record=record,
                event=event,
            )
            return

        if event.state == JobState.FAILED:
            self._apply_failed_event_locked(
                record=record,
                event=event,
            )

    # RÔLE :
    # Applique l'état running à un ticket.
    #
    # APPELÉE PAR :
    # - _apply_event_to_record_locked()
    #
    # MODIFIE :
    # - progression indéterminée ;
    # - suppression d'une ancienne erreur.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    @staticmethod
    def _apply_running_event_locked(
        record: JobRecord,
    ) -> None:
        record.progress_percent = None
        record.result = None
        record.error = None

    # RÔLE :
    # Enregistre le contenu et le modèle
    # d'une génération terminée.
    #
    # APPELÉE PAR :
    # - _apply_event_to_record_locked()
    #
    # MODIFIE :
    # - state ;
    # - progression à 100 % ;
    # - result ;
    # - error.
    #
    # SÉCURITÉ :
    # Un événement completed sans contenu ou modèle
    # est converti en état failed.
    #
    # PIPELINES :
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    def _apply_completed_event_locked(
        self,
        record: JobRecord,
        event: WorkerEventMessage,
    ) -> None:
        if not event.content or not event.model:
            self._mark_record_failed_locked(
                record=record,
                error=(
                    "Le worker a publié un résultat "
                    "incomplet pour ce job."
                ),
                occurred_at=event.occurred_at,
            )

            logger.error(
                "Résultat completed incomplet "
                "pour le job %s.",
                record.job_id,
            )
            return

        record.state = JobState.COMPLETED
        record.progress_percent = 100
        record.result = JobResult(
            content=event.content,
            model=event.model,
        )
        record.error = None

    # RÔLE :
    # Applique un événement failed au ticket.
    #
    # APPELÉE PAR :
    # - _apply_event_to_record_locked()
    #
    # APPELLE :
    # - _mark_record_failed_locked()
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    def _apply_failed_event_locked(
        self,
        record: JobRecord,
        event: WorkerEventMessage,
    ) -> None:
        error_message = (
            event.error
            or "Le worker a signalé un échec "
            "sans fournir de détail."
        )

        self._mark_record_failed_locked(
            record=record,
            error=error_message,
            occurred_at=event.occurred_at,
        )

    # RÔLE :
    # Centralise la modification d'un ticket
    # vers l'état failed.
    #
    # APPELÉE PAR :
    # - mark_publish_failed()
    # - _apply_completed_event_locked()
    # - _apply_failed_event_locked()
    #
    # MODIFIE :
    # - state → failed
    # - progress_percent → None
    # - result → None
    # - error
    # - updated_at
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _mark_record_failed_locked(
        record: JobRecord,
        error: str,
        occurred_at: datetime,
    ) -> None:
        record.state = JobState.FAILED
        record.progress_percent = None
        record.result = None
        record.error = error
        record.updated_at = occurred_at

    # RÔLE :
    # Retourne les tickets encore dans l'état queued,
    # triés selon leur date de création.
    #
    # APPELÉE PAR :
    # - _build_response_locked()
    #
    # RETOURNE :
    # - Liste ordonnée de JobRecord.
    #
    # IMPORTANT :
    # Cette fonction doit être appelée uniquement
    # pendant que self._lock est détenu.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _get_queued_jobs_locked(
        self,
    ) -> list[JobRecord]:
        return sorted(
            (
                candidate
                for candidate in self._jobs.values()
                if candidate.state == JobState.QUEUED
            ),
            key=lambda candidate: (
                candidate.created_at
            ),
        )

    # RÔLE :
    # Calcule la position d'un ticket
    # parmi les tickets encore en attente.
    #
    # APPELÉE PAR :
    # - _build_response_locked()
    #
    # RETOURNE :
    # - 1 pour le premier ticket queued ;
    # - None si le ticket n'est pas en attente.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    @staticmethod
    def _calculate_queue_position_locked(
        record: JobRecord,
        queued_jobs: list[JobRecord],
    ) -> int | None:
        if record.state != JobState.QUEUED:
            return None

        for position, candidate in enumerate(
            queued_jobs,
            start=1,
        ):
            if candidate.job_id == record.job_id:
                return position

        return None

    # RÔLE :
    # Convertit un JobRecord interne
    # en réponse publique Pydantic.
    #
    # APPELÉE PAR :
    # - create_job()
    # - get_job()
    #
    # APPELLE :
    # - _get_queued_jobs_locked()
    # - _calculate_queue_position_locked()
    #
    # RETOURNE :
    # - JobStatusResponse
    #
    # IMPORTANT :
    # Cette fonction doit être appelée uniquement
    # pendant que self._lock est détenu.
    #
    # PIPELINE :
    # - CHAT_JOB_STATUS
    def _build_response_locked(
        self,
        record: JobRecord,
    ) -> JobStatusResponse:
        queued_jobs = (
            self._get_queued_jobs_locked()
        )

        queue_position = (
            self._calculate_queue_position_locked(
                record=record,
                queued_jobs=queued_jobs,
            )
        )

        return JobStatusResponse(
            job_id=record.job_id,
            state=record.state,
            queue_position=queue_position,
            queue_total=len(queued_jobs),
            progress_percent=(
                record.progress_percent
            ),
            result=record.result,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )