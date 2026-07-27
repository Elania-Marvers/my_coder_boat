"""
FICHIER :
apps/api/app/broker.py

RÔLE GÉNÉRAL :
Centralise toutes les communications entre FastAPI
et RabbitMQ.

Le broker gère deux files :

1. mycoder.jobs
   FastAPI y publie les demandes de génération.
   Le worker Qwen consomme ces messages.

2. mycoder.events
   Le worker y publie les changements d'état
   et les résultats des jobs.
   FastAPI consomme ces événements.

CIRCULATION ALLER :
apps/api/app/main.py::create_job()
→ RabbitMQBroker.publish_job()
→ RabbitMQ mycoder.jobs
→ RabbitMQWorker.run()

CIRCULATION RETOUR :
RabbitMQWorker.publish_event()
→ RabbitMQ mycoder.events
→ RabbitMQBroker._handle_event()
→ JobStore.apply_event()

APPELÉ PAR :
- apps/api/app/main.py::lifespan()
- apps/api/app/main.py::_publish_job_or_raise()
- apps/api/app/main.py::health()

APPELLE :
- aio_pika.connect_robust()
- RabbitMQ
- apps/api/app/job_store.py::JobStore.apply_event()

PIPELINES :
- STARTUP
- CHAT_JOB_PUBLISH
- CHAT_JOB_EVENT
- CHAT_JOB_STATUS
- CHAT_JOB_FAILURE
"""

import logging
from typing import Final

import aio_pika
from aio_pika import (
    DeliveryMode,
    IncomingMessage,
    Message,
)
from pydantic import ValidationError

from .config import ApiSettings
from .job_store import JobStore
from .schemas import (
    WorkerEventMessage,
    WorkerJobMessage,
)


# Logger propre au broker RabbitMQ.
#
# Il permet de suivre :
# - les connexions ;
# - les publications ;
# - les événements reçus ;
# - les erreurs de validation ou de traitement.
logger = logging.getLogger(__name__)


# Arguments appliqués à la file mycoder.jobs.
#
# x-single-active-consumer demande à RabbitMQ
# de ne livrer les messages qu'à un seul worker actif.
#
# Ce mécanisme complète le prefetch_count=1
# configuré dans rabbitmq_worker.py.
JOB_QUEUE_ARGUMENTS: Final[dict[str, bool]] = {
    "x-single-active-consumer": True,
}


# RÔLE :
# Gère la connexion RabbitMQ utilisée par FastAPI.
#
# INSTANCIÉE PAR :
# - apps/api/app/main.py
#
# UTILISÉE PAR :
# - apps/api/app/main.py::lifespan()
# - apps/api/app/main.py::_publish_job_or_raise()
# - apps/api/app/main.py::health()
#
# RESPONSABILITÉS :
# - ouvrir et fermer RabbitMQ ;
# - déclarer les files ;
# - publier les jobs ;
# - consommer les événements ;
# - transmettre les événements au JobStore.
#
# PIPELINES :
# - STARTUP
# - CHAT_JOB_PUBLISH
# - CHAT_JOB_EVENT
class RabbitMQBroker:
    # RÔLE :
    # Enregistre la configuration et le stockage
    # d'états utilisés par le broker.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py
    #
    # REÇOIT :
    # - ApiSettings contenant les noms des files
    #   et l'adresse RabbitMQ ;
    # - JobStore recevant les événements du worker.
    #
    # PIPELINE :
    # - STARTUP
    def __init__(
        self,
        settings: ApiSettings,
        job_store: JobStore,
    ) -> None:
        self.settings = settings
        self.job_store = job_store

        # Connexion TCP robuste vers RabbitMQ.
        #
        # connect_robust permet à aio-pika
        # de tenter une reconnexion après une coupure.
        self.connection: (
            aio_pika.abc.AbstractRobustConnection
            | None
        ) = None

        # Canal AMQP utilisé pour déclarer les files,
        # publier les messages et lancer le consommateur.
        self.channel: (
            aio_pika.abc.AbstractRobustChannel
            | None
        ) = None

        # Référence vers la file contenant
        # les demandes destinées au worker.
        self.job_queue: (
            aio_pika.abc.AbstractQueue
            | None
        ) = None

        # Référence vers la file contenant
        # les événements publiés par le worker.
        self.event_queue: (
            aio_pika.abc.AbstractQueue
            | None
        ) = None

    # RÔLE :
    # Ouvre RabbitMQ, crée le canal, déclare les files
    # puis démarre la consommation des événements.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py::lifespan()
    #
    # APPELLE :
    # - aio_pika.connect_robust()
    # - _declare_job_queue()
    # - _declare_event_queue()
    # - _start_event_consumer()
    #
    # MODIFIE :
    # - self.connection
    # - self.channel
    # - self.job_queue
    # - self.event_queue
    #
    # ERREUR :
    # Une exception remonte jusqu'au lifespan
    # lorsque RabbitMQ est indisponible.
    #
    # PIPELINE :
    # - STARTUP
    async def connect(self) -> None:
        # Évite d'ouvrir une deuxième connexion
        # si le broker est déjà utilisable.
        if self.is_connected():
            logger.warning(
                "Le broker RabbitMQ est déjà connecté."
            )
            return

        logger.info(
            "Connexion au broker RabbitMQ."
        )

        self.connection = (
            await aio_pika.connect_robust(
                self.settings.rabbitmq_url
            )
        )

        # publisher_confirms demande à RabbitMQ
        # de confirmer les publications.
        #
        # Une publication refusée ou interrompue
        # peut ainsi remonter sous forme d'exception.
        self.channel = (
            await self.connection.channel(
                publisher_confirms=True
            )
        )

        self.job_queue = (
            await self._declare_job_queue()
        )

        self.event_queue = (
            await self._declare_event_queue()
        )

        await self._start_event_consumer()

        logger.info(
            "Broker RabbitMQ prêt : jobs=%s, events=%s.",
            self.settings.rabbitmq_job_queue,
            self.settings.rabbitmq_event_queue,
        )

    # RÔLE :
    # Ferme proprement la connexion RabbitMQ
    # puis retire les références devenues inutilisables.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py::lifespan()
    #
    # APPELLE :
    # - aio-pika AbstractRobustConnection.close()
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
                "Fermeture de la connexion RabbitMQ."
            )

            await self.connection.close()

        # Supprime les références afin qu'aucune autre
        # méthode ne considère l'ancien canal comme valide.
        self.connection = None
        self.channel = None
        self.job_queue = None
        self.event_queue = None

    # RÔLE :
    # Indique si la connexion et le canal RabbitMQ
    # sont actuellement ouverts.
    #
    # APPELÉE PAR :
    # - connect()
    # - apps/api/app/main.py::health()
    #
    # RETOURNE :
    # - True lorsque la connexion et le canal sont ouverts ;
    # - False dans tous les autres cas.
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
    # Retourne le canal RabbitMQ ouvert
    # ou interrompt l'opération avec une erreur explicite.
    #
    # APPELÉE PAR :
    # - _declare_job_queue()
    # - _declare_event_queue()
    # - publish_job()
    #
    # RETOURNE :
    # - AbstractRobustChannel
    #
    # ERREUR :
    # - RuntimeError si connect() n'a pas été appelée
    #   ou si le canal est déjà fermé.
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_PUBLISH
    def _get_channel_or_raise(
        self,
    ) -> aio_pika.abc.AbstractRobustChannel:
        if (
            self.channel is None
            or self.channel.is_closed
        ):
            raise RuntimeError(
                "Le canal RabbitMQ de FastAPI "
                "n'est pas disponible."
            )

        return self.channel

    # RÔLE :
    # Déclare la file contenant les demandes
    # destinées au worker Qwen.
    #
    # APPELÉE PAR :
    # - connect()
    #
    # APPELLE :
    # - _get_channel_or_raise()
    # - aio-pika channel.declare_queue()
    #
    # CONFIGURATION :
    # - durable=True :
    #   la définition de la file survit
    #   au redémarrage de RabbitMQ ;
    #
    # - x-single-active-consumer=True :
    #   un seul worker reçoit les tickets.
    #
    # RETOURNE :
    # - La file mycoder.jobs déclarée.
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_PUBLISH
    async def _declare_job_queue(
        self,
    ) -> aio_pika.abc.AbstractQueue:
        channel = self._get_channel_or_raise()

        logger.info(
            "Déclaration de la file de jobs %s.",
            self.settings.rabbitmq_job_queue,
        )

        return await channel.declare_queue(
            self.settings.rabbitmq_job_queue,
            durable=True,
            arguments=JOB_QUEUE_ARGUMENTS,
        )

    # RÔLE :
    # Déclare la file contenant les états
    # et les résultats publiés par le worker.
    #
    # APPELÉE PAR :
    # - connect()
    #
    # APPELLE :
    # - _get_channel_or_raise()
    # - aio-pika channel.declare_queue()
    #
    # CONFIGURATION :
    # - durable=True :
    #   la définition de la file survit
    #   au redémarrage de RabbitMQ.
    #
    # RETOURNE :
    # - La file mycoder.events déclarée.
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_EVENT
    async def _declare_event_queue(
        self,
    ) -> aio_pika.abc.AbstractQueue:
        channel = self._get_channel_or_raise()

        logger.info(
            "Déclaration de la file d'événements %s.",
            self.settings.rabbitmq_event_queue,
        )

        return await channel.declare_queue(
            self.settings.rabbitmq_event_queue,
            durable=True,
        )

    # RÔLE :
    # Associe la file mycoder.events
    # à la méthode chargée de traiter chaque événement.
    #
    # APPELÉE PAR :
    # - connect()
    #
    # APPELLE :
    # - RabbitMQ queue.consume()
    # - _handle_event() pour chaque message reçu
    #
    # ERREUR :
    # - RuntimeError si la file n'est pas déclarée.
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_EVENT
    async def _start_event_consumer(
        self,
    ) -> None:
        if self.event_queue is None:
            raise RuntimeError(
                "La file d'événements RabbitMQ "
                "n'est pas initialisée."
            )

        await self.event_queue.consume(
            self._handle_event,
            no_ack=False,
        )

        logger.info(
            "Consommation de la file %s démarrée.",
            self.settings.rabbitmq_event_queue,
        )

    # RÔLE :
    # Transforme un WorkerJobMessage en message AMQP
    # persistant destiné à RabbitMQ.
    #
    # APPELÉE PAR :
    # - publish_job()
    #
    # APPELLE :
    # - WorkerJobMessage.model_dump_json()
    # - aio_pika.Message()
    #
    # RETOURNE :
    # - Message AMQP prêt à être publié.
    #
    # PIPELINE :
    # - CHAT_JOB_PUBLISH
    @staticmethod
    def _build_job_message(
        job: WorkerJobMessage,
    ) -> Message:
        return Message(
            body=job.model_dump_json().encode(
                "utf-8"
            ),
            content_type="application/json",

            # Demande à RabbitMQ de conserver
            # le message sur disque lorsque possible.
            delivery_mode=DeliveryMode.PERSISTENT,

            # Ces deux identifiants facilitent
            # le suivi du même job entre les services.
            message_id=str(job.job_id),
            correlation_id=str(job.job_id),
        )

    # RÔLE :
    # Publie un ticket dans la file mycoder.jobs.
    #
    # APPELÉE PAR :
    # - apps/api/app/main.py::_publish_job_or_raise()
    # - apps/api/app/main.py::create_job()
    #
    # APPELLE :
    # - _get_channel_or_raise()
    # - _build_job_message()
    # - RabbitMQ default exchange.publish()
    #
    # PRODUIT :
    # - Un message WorkerJobMessage dans mycoder.jobs.
    #
    # CONSOMMÉ PAR :
    # - apps/worker/src/local_qwen_worker/
    #   rabbitmq_worker.py::RabbitMQWorker.run()
    #
    # ERREUR :
    # Une erreur remonte jusqu'à main.py,
    # qui marque alors le ticket comme failed
    # et retourne une réponse HTTP 503.
    #
    # PIPELINES :
    # - CHAT_JOB_PUBLISH
    # - CHAT_JOB_FAILURE
    async def publish_job(
        self,
        job: WorkerJobMessage,
    ) -> None:
        channel = self._get_channel_or_raise()

        message = self._build_job_message(
            job
        )

        # Le default exchange route un message
        # directement vers une file portant
        # le même nom que la routing_key.
        await channel.default_exchange.publish(
            message,
            routing_key=(
                self.settings.rabbitmq_job_queue
            ),
        )

        logger.info(
            "Job %s publié dans la file %s.",
            job.job_id,
            self.settings.rabbitmq_job_queue,
        )

    # RÔLE :
    # Valide le contenu JSON d'un événement RabbitMQ.
    #
    # APPELÉE PAR :
    # - _handle_event()
    #
    # APPELLE :
    # - WorkerEventMessage.model_validate_json()
    #
    # RETOURNE :
    # - WorkerEventMessage si le message est valide ;
    # - None lorsque le message est inutilisable.
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _parse_event(
        message: IncomingMessage,
    ) -> WorkerEventMessage | None:
        try:
            return (
                WorkerEventMessage
                .model_validate_json(
                    message.body
                )
            )

        except ValidationError:
            logger.exception(
                "Événement RabbitMQ invalide. "
                "Le message sera rejeté."
            )

            return None

    # RÔLE :
    # Traite un événement reçu depuis mycoder.events.
    #
    # APPELÉE PAR :
    # - RabbitMQ automatiquement après
    #   _start_event_consumer()
    #
    # APPELLE :
    # - _parse_event()
    # - apps/api/app/job_store.py
    #   ::JobStore.apply_event()
    # - IncomingMessage.ack()
    # - IncomingMessage.reject()
    # - IncomingMessage.nack()
    #
    # COMPORTEMENT :
    # - JSON invalide :
    #   le message est rejeté sans remise en file ;
    #
    # - erreur pendant l'application :
    #   le message est replacé dans la file ;
    #
    # - événement appliqué :
    #   le message est acquitté.
    #
    # MODIFIE :
    # - L'état du ticket dans JobStore.
    #
    # PIPELINES :
    # - CHAT_JOB_EVENT
    # - CHAT_JOB_STATUS
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    async def _handle_event(
        self,
        message: IncomingMessage,
    ) -> None:
        event = self._parse_event(
            message
        )

        # Un événement invalide ne pourra jamais
        # devenir valide après une nouvelle livraison.
        if event is None:
            await message.reject(
                requeue=False
            )
            return

        try:
            await self.job_store.apply_event(
                event
            )

        except Exception:
            logger.exception(
                "Impossible d'appliquer l'événement "
                "du job %s dans JobStore.",
                event.job_id,
            )

            # Une erreur temporaire de stockage
            # peut justifier une nouvelle tentative.
            await message.nack(
                requeue=True
            )
            return

        # RabbitMQ peut retirer définitivement
        # le message de la file d'événements.
        await message.ack()

        logger.info(
            "Événement %s appliqué au job %s.",
            event.state,
            event.job_id,
        )