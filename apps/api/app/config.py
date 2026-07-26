"""
FICHIER :
apps/api/app/config.py

RÔLE GÉNÉRAL :
Charge, valide et centralise la configuration
RabbitMQ utilisée par l'API FastAPI.

Les valeurs proviennent, dans cet ordre :

1. des variables d'environnement du processus ;
2. du fichier .env situé à la racine du dépôt ;
3. des valeurs par défaut définies ci-dessous.

UTILISÉ PAR :
- apps/api/app/main.py
- apps/api/app/broker.py

CIRCULATION CONCERNÉE :

Aller :
FastAPI
→ RabbitMQ mycoder.jobs
→ worker Qwen

Retour :
worker Qwen
→ RabbitMQ mycoder.events
→ FastAPI

PARAMÈTRES GÉRÉS :
- adresse AMQP de RabbitMQ ;
- nom de la file des jobs ;
- nom de la file des événements.

PIPELINES :
- STARTUP
- CHAT_JOB_PUBLISH
- CHAT_JOB_EVENT
"""

from functools import lru_cache
from typing import Self

from pydantic import (
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


# RÔLE :
# Regroupe les paramètres RabbitMQ nécessaires
# au fonctionnement de l'API FastAPI.
#
# INSTANCIÉE PAR :
# - get_settings()
# - éventuellement les tests unitaires
#
# UTILISÉE PAR :
# - apps/api/app/main.py
# - apps/api/app/broker.py::RabbitMQBroker
#
# SOURCES :
# - variables d'environnement ;
# - fichier .env ;
# - valeurs par défaut.
#
# PIPELINES :
# - STARTUP
# - CHAT_JOB_PUBLISH
# - CHAT_JOB_EVENT
class ApiSettings(BaseSettings):
    # Configure la lecture du fichier .env
    # et des variables d'environnement.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",

        # Autorise aussi bien RABBITMQ_URL
        # que rabbitmq_url.
        case_sensitive=False,

        # Ignore les variables appartenant
        # à Django, Ollama ou au worker.
        extra="ignore",
    )

    # Adresse AMQP utilisée par FastAPI
    # pour joindre RabbitMQ.
    #
    # VARIABLE :
    # RABBITMQ_URL
    rabbitmq_url: str = Field(
        default=(
            "amqp://guest:guest@127.0.0.1:5672/"
        ),
        min_length=1,
        max_length=2_000,
    )

    # File dans laquelle FastAPI publie
    # les demandes de génération.
    #
    # CONSOMMÉE PAR :
    # apps/worker/src/local_qwen_worker/
    # rabbitmq_worker.py::RabbitMQWorker.run()
    #
    # VARIABLE :
    # RABBITMQ_JOB_QUEUE
    rabbitmq_job_queue: str = Field(
        default="mycoder.jobs",
        min_length=1,
        max_length=255,
    )

    # File dans laquelle le worker publie
    # les états et les résultats des jobs.
    #
    # CONSOMMÉE PAR :
    # apps/api/app/broker.py
    # ::RabbitMQBroker._handle_event()
    #
    # VARIABLE :
    # RABBITMQ_EVENT_QUEUE
    rabbitmq_event_queue: str = Field(
        default="mycoder.events",
        min_length=1,
        max_length=255,
    )

    # RÔLE :
    # Nettoie les chaînes provenant
    # du fichier .env ou de l'environnement.
    #
    # APPELÉE PAR :
    # - Pydantic pendant ApiSettings()
    #
    # CHAMPS CONCERNÉS :
    # - rabbitmq_url
    # - rabbitmq_job_queue
    # - rabbitmq_event_queue
    #
    # RETOURNE :
    # - La chaîne sans espaces superflus.
    #
    # PIPELINE :
    # - STARTUP
    @field_validator(
        "rabbitmq_url",
        "rabbitmq_job_queue",
        "rabbitmq_event_queue",
        mode="before",
    )
    @classmethod
    def strip_text_values(
        cls,
        value: object,
    ) -> object:
        if isinstance(value, str):
            return value.strip()

        return value

    # RÔLE :
    # Vérifie que l'adresse RabbitMQ utilise
    # le protocole AMQP ou AMQPS.
    #
    # APPELÉE PAR :
    # - Pydantic pendant ApiSettings()
    #
    # ERREUR :
    # - ValueError pour une adresse ne commençant
    #   ni par amqp:// ni par amqps://.
    #
    # PIPELINE :
    # - STARTUP
    @field_validator(
        "rabbitmq_url",
        mode="after",
    )
    @classmethod
    def validate_rabbitmq_url(
        cls,
        value: str,
    ) -> str:
        if not value.startswith(
            (
                "amqp://",
                "amqps://",
            )
        ):
            raise ValueError(
                "RABBITMQ_URL doit commencer "
                "par amqp:// ou amqps://."
            )

        return value

    # RÔLE :
    # Vérifie qu'un nom de file RabbitMQ
    # peut être utilisé par l'application.
    #
    # APPELÉE PAR :
    # - Pydantic pendant ApiSettings()
    #
    # REFUSE :
    # - les espaces et retours à la ligne ;
    # - les noms réservés commençant par amq.
    #
    # PIPELINE :
    # - STARTUP
    @field_validator(
        "rabbitmq_job_queue",
        "rabbitmq_event_queue",
        mode="after",
    )
    @classmethod
    def validate_queue_name(
        cls,
        value: str,
    ) -> str:
        if any(
            character.isspace()
            for character in value
        ):
            raise ValueError(
                "Un nom de file RabbitMQ "
                "ne peut pas contenir d'espace."
            )

        if value.startswith("amq."):
            raise ValueError(
                "Le préfixe `amq.` est réservé "
                "par RabbitMQ."
            )

        return value

    # RÔLE :
    # Vérifie que les demandes et les événements
    # utilisent deux files différentes.
    #
    # APPELÉE PAR :
    # - Pydantic après validation des champs
    #
    # RAISON :
    # Utiliser une seule file mélangerait les tickets
    # destinés au worker et les résultats destinés à FastAPI.
    #
    # PIPELINE :
    # - STARTUP
    @model_validator(mode="after")
    def validate_queue_names_are_distinct(
        self,
    ) -> Self:
        if (
            self.rabbitmq_job_queue
            == self.rabbitmq_event_queue
        ):
            raise ValueError(
                "RABBITMQ_JOB_QUEUE et "
                "RABBITMQ_EVENT_QUEUE doivent "
                "désigner deux files différentes."
            )

        return self


# RÔLE :
# Charge la configuration FastAPI une seule fois.
#
# APPELÉE PAR :
# - apps/api/app/main.py
#
# APPELLE :
# - ApiSettings()
#
# RETOURNE :
# - La même instance ApiSettings pendant
#   toute la durée du processus FastAPI.
#
# IMPORTANT :
# Une modification du fichier .env nécessite
# un redémarrage de FastAPI pour être appliquée.
#
# PIPELINE :
# - STARTUP
@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()