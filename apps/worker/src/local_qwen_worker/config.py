"""
FICHIER :
apps/worker/src/local_qwen_worker/config.py

RÔLE GÉNÉRAL :
Charge, valide et centralise toute la configuration
utilisée par le worker Qwen.

Les valeurs proviennent principalement :

1. des variables d'environnement ;
2. du fichier .env situé à la racine du projet ;
3. des valeurs par défaut définies dans WorkerSettings.

UTILISÉ PAR :
- apps/worker/src/local_qwen_worker/
  rabbitmq_worker.py::RabbitMQWorker

- apps/worker/src/local_qwen_worker/
  ollama_client.py::OllamaClient

- apps/worker/src/local_qwen_worker/
  service.py::QwenService indirectement

PARAMÈTRES GÉRÉS :
- modèle Qwen ;
- taille du contexte ;
- température ;
- adresse Ollama ;
- délai Ollama ;
- durée de conservation du modèle en mémoire ;
- adresse RabbitMQ ;
- noms des files RabbitMQ.

PIPELINES :
- STARTUP
- CHAT_JOB_CONSUME
- CHAT_JOB_GENERATE
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
# Regroupe tous les paramètres nécessaires
# au démarrage et au fonctionnement du worker.
#
# INSTANCIÉE PAR :
# - get_settings()
# - éventuellement les tests unitaires
#
# UTILISÉE PAR :
# - RabbitMQWorker.__init__()
# - OllamaClient.__init__()
#
# SOURCES :
# - environnement du processus ;
# - fichier .env ;
# - valeurs par défaut ci-dessous.
#
# PIPELINES :
# - STARTUP
# - CHAT_JOB_CONSUME
# - CHAT_JOB_GENERATE
class WorkerSettings(BaseSettings):
    # Configure la lecture des variables d'environnement.
    #
    # env_file=".env" fonctionne ici parce que
    # les commandes du Makefile sont lancées
    # depuis la racine du dépôt.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",

        # Accepte aussi bien QWEN_MODEL
        # que qwen_model.
        case_sensitive=False,

        # Ignore les variables appartenant
        # à Django ou FastAPI.
        extra="ignore",
    )

    # Nom du modèle demandé à Ollama.
    #
    # VARIABLE :
    # QWEN_MODEL
    qwen_model: str = Field(
        default="qwen2.5-coder:7b-instruct-q4_K_M",
        min_length=1,
        max_length=500,
    )

    # Taille maximale du contexte transmis au modèle.
    #
    # VARIABLE :
    # QWEN_CONTEXT
    qwen_context: int = Field(
        default=8192,
        ge=512,
        le=131_072,
    )

    # Température de génération.
    #
    # Une valeur faible rend généralement
    # les réponses plus stables et prévisibles.
    #
    # VARIABLE :
    # QWEN_TEMPERATURE
    qwen_temperature: float = Field(
        default=0.25,
        ge=0.0,
        le=2.0,
    )

    # Adresse HTTP du serveur Ollama local.
    #
    # VARIABLE :
    # OLLAMA_BASE_URL
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434",
        min_length=1,
        max_length=2_000,
    )

    # Durée maximale d'un appel complet vers Ollama.
    #
    # Cette valeur peut être élevée puisque
    # la génération locale dure parfois plusieurs minutes.
    #
    # VARIABLE :
    # OLLAMA_TIMEOUT_SECONDS
    ollama_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        le=3_600,
    )

    # Durée pendant laquelle Ollama conserve
    # le modèle chargé après la génération.
    #
    # VARIABLE :
    # OLLAMA_KEEP_ALIVE
    ollama_keep_alive: str = Field(
        default="10m",
        min_length=1,
        max_length=100,
    )

    # Adresse AMQP utilisée par le worker
    # pour rejoindre RabbitMQ.
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

    # File contenant les tickets publiés
    # par l'API FastAPI.
    #
    # VARIABLE :
    # RABBITMQ_JOB_QUEUE
    rabbitmq_job_queue: str = Field(
        default="mycoder.jobs",
        min_length=1,
        max_length=255,
    )

    # File contenant les états et résultats
    # publiés par le worker.
    #
    # VARIABLE :
    # RABBITMQ_EVENT_QUEUE
    rabbitmq_event_queue: str = Field(
        default="mycoder.events",
        min_length=1,
        max_length=255,
    )

    # RÔLE :
    # Nettoie les champs textuels avant
    # leur validation définitive.
    #
    # APPELÉE PAR :
    # - Pydantic pendant WorkerSettings()
    #
    # CHAMPS CONCERNÉS :
    # - qwen_model
    # - ollama_base_url
    # - ollama_keep_alive
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
        "qwen_model",
        "ollama_base_url",
        "ollama_keep_alive",
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
    # Retire le slash final de l'adresse Ollama.
    #
    # APPELÉE PAR :
    # - Pydantic pendant WorkerSettings()
    #
    # RAISON :
    # Le client Ollama reçoit une adresse stable comme :
    #
    # http://127.0.0.1:11434
    #
    # plutôt que plusieurs variantes avec des slashs.
    #
    # PIPELINE :
    # - STARTUP
    @field_validator(
        "ollama_base_url",
        mode="after",
    )
    @classmethod
    def normalise_ollama_base_url(
        cls,
        value: str,
    ) -> str:
        return value.rstrip("/")

    # RÔLE :
    # Vérifie que l'adresse Ollama utilise
    # un protocole HTTP pris en charge.
    #
    # APPELÉE PAR :
    # - Pydantic pendant WorkerSettings()
    #
    # ERREUR :
    # - ValueError pour une adresse ne commençant
    #   ni par http:// ni par https://.
    #
    # PIPELINE :
    # - STARTUP
    @field_validator(
        "ollama_base_url",
        mode="after",
    )
    @classmethod
    def validate_ollama_base_url(
        cls,
        value: str,
    ) -> str:
        if not value.startswith(
            (
                "http://",
                "https://",
            )
        ):
            raise ValueError(
                "OLLAMA_BASE_URL doit commencer "
                "par http:// ou https://."
            )

        return value

    # RÔLE :
    # Vérifie que l'adresse RabbitMQ utilise
    # un protocole AMQP accepté.
    #
    # APPELÉE PAR :
    # - Pydantic pendant WorkerSettings()
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
    # Vérifie la forme des noms de files RabbitMQ.
    #
    # APPELÉE PAR :
    # - Pydantic pendant WorkerSettings()
    #
    # REFUSE :
    # - les espaces ;
    # - les retours à la ligne ;
    # - les noms commençant par amq.
    #
    # RAISON :
    # Le préfixe amq. est réservé aux noms
    # utilisés en interne par RabbitMQ.
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
    # Vérifie la cohérence entre les deux files.
    #
    # APPELÉE PAR :
    # - Pydantic après la validation
    #   de tous les champs.
    #
    # ERREUR :
    # Les tickets et les événements ne peuvent pas
    # utiliser exactement la même file.
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
# Charge la configuration du worker une seule fois.
#
# APPELÉE PAR :
# - RabbitMQWorker.__init__()
# - OllamaClient.__init__()
#
# APPELLE :
# - WorkerSettings()
#
# RETOURNE :
# - La même instance WorkerSettings
#   pendant toute la durée du processus.
#
# IMPORTANT :
# Après une modification du fichier .env,
# le worker doit être redémarré pour recharger
# les nouvelles valeurs.
#
# PIPELINE :
# - STARTUP
@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    return WorkerSettings()