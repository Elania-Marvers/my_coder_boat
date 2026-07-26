"""
FICHIER :
apps/api/app/main.py

RÔLE GÉNÉRAL :
Déclare l'application FastAPI et expose les routes HTTP
utilisées par le front Django.

Ce fichier coordonne trois éléments :

1. la validation HTTP avec FastAPI et Pydantic ;
2. le stockage temporaire des états avec JobStore ;
3. la communication RabbitMQ avec RabbitMQBroker.

APPELÉ PAR :
- Uvicorn avec app.main:app
- Le Makefile, cibles api et dev

APPELLE :
- apps/api/app/config.py::get_settings()
- apps/api/app/job_store.py::JobStore
- apps/api/app/broker.py::RabbitMQBroker
- apps/api/app/schemas.py

ROUTES EXPOSÉES :
- GET  /health
- POST /v1/jobs
- GET  /v1/jobs/{job_id}

PIPELINES :
- STARTUP
- CHAT_JOB_CREATE
- CHAT_JOB_PUBLISH
- CHAT_JOB_STATUS
- CHAT_JOB_FAILURE
"""

import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import (
    FastAPI,
    HTTPException,
    status,
)

from .broker import RabbitMQBroker
from .config import get_settings
from .job_store import JobStore
from .schemas import (
    CreateJobRequest,
    JobStatusResponse,
    WorkerJobMessage,
)


# Logger propre à l'API.
#
# Il est notamment utilisé pour conserver
# les détails techniques d'une erreur RabbitMQ
# sans les exposer directement au navigateur.
logger = logging.getLogger(__name__)


# Charge la configuration de l'API une seule fois.
#
# APPELLE :
# apps/api/app/config.py::get_settings()
settings = get_settings()


# Stocke les états des tickets connus par FastAPI.
#
# IMPORTANT :
# Ce stockage est actuellement conservé en mémoire.
# Il est donc réinitialisé lorsque FastAPI redémarre.
#
# IMPLÉMENTATION :
# apps/api/app/job_store.py::JobStore
job_store = JobStore()


# Gère la connexion RabbitMQ partagée par l'API.
#
# Le broker :
# - publie les tickets dans mycoder.jobs ;
# - consomme les événements dans mycoder.events ;
# - transmet ces événements au JobStore.
#
# IMPLÉMENTATION :
# apps/api/app/broker.py::RabbitMQBroker
broker = RabbitMQBroker(
    settings=settings,
    job_store=job_store,
)


# RÔLE :
# Ouvre la connexion RabbitMQ avant que FastAPI
# accepte les premières requêtes HTTP.
#
# APPELÉE PAR :
# - FastAPI au démarrage de l'application ;
# - FastAPI à l'arrêt de l'application.
#
# APPELLE :
# - apps/api/app/broker.py::RabbitMQBroker.connect()
# - apps/api/app/broker.py::RabbitMQBroker.close()
#
# EFFETS DE BORD :
# - ouvre une connexion RabbitMQ ;
# - déclare les files mycoder.jobs et mycoder.events ;
# - démarre la consommation des événements du worker.
#
# ERREUR :
# Si RabbitMQ est indisponible, le démarrage de FastAPI échoue.
#
# PIPELINE :
# - STARTUP
@asynccontextmanager
async def lifespan(
    _: FastAPI,
):
    logger.info(
        "Connexion de FastAPI à RabbitMQ."
    )

    await broker.connect()

    try:
        yield

    finally:
        logger.info(
            "Fermeture de la connexion RabbitMQ de FastAPI."
        )

        await broker.close()


# Crée l'instance principale de FastAPI.
#
# UTILISÉE PAR :
# - uvicorn app.main:app
#
# Le lifespan défini ci-dessus est automatiquement
# exécuté au démarrage et à l'arrêt du serveur.
app = FastAPI(
    title="MyCoder API",
    version="0.2.0",
    description=(
        "API locale de coordination entre Django, "
        "RabbitMQ et le worker Qwen."
    ),
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# RÔLE :
# Transforme une requête HTTP validée par Pydantic
# en message destiné au worker RabbitMQ.
#
# APPELÉE PAR :
# - create_job()
#
# REÇOIT :
# - l'identifiant UUID généré par FastAPI ;
# - la conversation validée par CreateJobRequest.
#
# RETOURNE :
# - WorkerJobMessage
#
# CONSOMMÉ ENSUITE PAR :
# apps/worker/src/local_qwen_worker/rabbitmq_worker.py
# ::WorkerJobMessage
#
# PIPELINE :
# - CHAT_JOB_CREATE
# - CHAT_JOB_PUBLISH
def _build_worker_job_message(
    job_id: UUID,
    request: CreateJobRequest,
) -> WorkerJobMessage:
    return WorkerJobMessage(
        job_id=job_id,
        messages=request.messages,
    )


# RÔLE :
# Publie un ticket dans RabbitMQ et transforme
# une erreur technique en réponse HTTP contrôlée.
#
# APPELÉE PAR :
# - create_job()
#
# APPELLE :
# - apps/api/app/broker.py
#   ::RabbitMQBroker.publish_job()
# - apps/api/app/job_store.py
#   ::JobStore.mark_publish_failed()
#
# MODIFIE :
# - La file RabbitMQ mycoder.jobs ;
# - l'état du ticket en cas d'échec.
#
# ERREUR HTTP :
# - 503 lorsque RabbitMQ refuse ou ne reçoit pas le ticket.
#
# PIPELINES :
# - CHAT_JOB_PUBLISH
# - CHAT_JOB_FAILURE
async def _publish_job_or_raise(
    job_id: UUID,
    job_message: WorkerJobMessage,
) -> None:
    try:
        await broker.publish_job(
            job_message
        )

    except Exception as exc:
        error_message = (
            "Impossible de publier le job "
            "dans RabbitMQ."
        )

        # Le ticket existe déjà dans JobStore.
        # Il est donc marqué comme failed avant
        # de retourner l'erreur HTTP.
        await job_store.mark_publish_failed(
            job_id=job_id,
            error=error_message,
        )

        logger.exception(
            "Échec de la publication RabbitMQ "
            "pour le job %s.",
            job_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=error_message,
        ) from exc


# RÔLE :
# Récupère un ticket depuis JobStore
# ou lève une erreur HTTP contrôlée.
#
# APPELÉE PAR :
# - create_job()
# - get_job()
#
# APPELLE :
# - apps/api/app/job_store.py::JobStore.get_job()
#
# RETOURNE :
# - JobStatusResponse
#
# ERREUR HTTP :
# - statut fourni par l'appelant lorsque le ticket
#   est absent du stockage.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
async def _get_job_or_raise(
    job_id: UUID,
    *,
    status_code: int,
    detail: str,
) -> JobStatusResponse:
    job = await job_store.get_job(
        job_id
    )

    if job is None:
        raise HTTPException(
            status_code=status_code,
            detail=detail,
        )

    return job


# RÔLE :
# Vérifie que FastAPI fonctionne et indique
# si la connexion RabbitMQ est actuellement ouverte.
#
# APPELÉE PAR :
# - Makefile::api-check
# - Makefile::dev pendant le démarrage
# - Requête GET /health
#
# APPELLE :
# - apps/api/app/broker.py
#   ::RabbitMQBroker.is_connected()
#
# RETOURNE :
# {
#     "status": "ok",
#     "service": "mycoder-api",
#     "rabbitmq_connected": true
# }
#
# PIPELINE :
# - STARTUP
@app.get(
    "/health",
    response_model=dict[str, str | bool],
)
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": "mycoder-api",
        "rabbitmq_connected": (
            broker.is_connected()
        ),
    }


# RÔLE :
# Crée un nouveau ticket pour une conversation
# envoyée par le front Django.
#
# APPELÉE PAR :
# - Requête POST /v1/jobs
# - apps/front/chat/services.py::create_job()
#
# APPELLE :
# - uuid.uuid4()
# - apps/api/app/job_store.py
#   ::JobStore.create_job()
# - _build_worker_job_message()
# - _publish_job_or_raise()
# - _get_job_or_raise()
#
# MODIFIE :
# - Le stockage mémoire JobStore ;
# - la file RabbitMQ mycoder.jobs.
#
# RETOURNE :
# - HTTP 202 avec JobStatusResponse.
#
# ERREURS HTTP :
# - 422 si Pydantic refuse la conversation ;
# - 503 si RabbitMQ est indisponible ;
# - 500 si le ticket disparaît anormalement du JobStore.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_PUBLISH
@app.post(
    "/v1/jobs",
    response_model=JobStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_job(
    request: CreateJobRequest,
) -> JobStatusResponse:
    # Chaque ticket reçoit un identifiant unique
    # utilisé dans Django, RabbitMQ et le worker.
    job_id = uuid4()

    logger.info(
        "Création du job %s.",
        job_id,
    )

    # Le ticket est d'abord enregistré dans JobStore
    # avec l'état queued et une progression de 0 %.
    await job_store.create_job(
        job_id
    )

    # Prépare le message JSON qui sera publié
    # dans la file RabbitMQ mycoder.jobs.
    job_message = _build_worker_job_message(
        job_id=job_id,
        request=request,
    )

    # Publie le message ou retourne une erreur 503.
    await _publish_job_or_raise(
        job_id=job_id,
        job_message=job_message,
    )

    # Relit l'état après la publication.
    #
    # Le worker peut éventuellement avoir commencé
    # le traitement très rapidement.
    return await _get_job_or_raise(
        job_id,
        status_code=(
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        detail=(
            "Le job créé est introuvable "
            "dans le stockage FastAPI."
        ),
    )


# RÔLE :
# Retourne l'état courant d'un ticket.
#
# APPELÉE PAR :
# - Requête GET /v1/jobs/{job_id}
# - apps/front/chat/services.py::get_job_status()
#
# APPELLE :
# - _get_job_or_raise()
# - apps/api/app/job_store.py::JobStore.get_job()
#
# RETOURNE :
# - queued avec sa position ;
# - running avec une progression indéterminée ;
# - completed avec le résultat Qwen ;
# - failed avec le message d'erreur.
#
# ERREUR HTTP :
# - 404 si FastAPI ne connaît pas l'identifiant.
#
# PIPELINE :
# - CHAT_JOB_STATUS
@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
)
async def get_job(
    job_id: UUID,
) -> JobStatusResponse:
    return await _get_job_or_raise(
        job_id,
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job introuvable.",
    )