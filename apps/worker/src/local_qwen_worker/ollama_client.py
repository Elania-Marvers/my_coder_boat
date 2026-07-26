"""
FICHIER :
apps/worker/src/local_qwen_worker/ollama_client.py

RÔLE GÉNÉRAL :
Centralise toutes les communications directes
entre le worker MyCoder et le serveur Ollama local.

Ce fichier constitue la dernière couche Python
avant l'exécution du modèle Qwen.

CIRCULATION ALLER :
RabbitMQWorker._generate_response()
→ QwenService.chat()
→ OllamaClient.chat()
→ OllamaClient._request_chat()
→ Ollama /api/chat
→ modèle Qwen

CIRCULATION RETOUR :
Ollama
→ réponse du modèle
→ OllamaClient._parse_response()
→ ChatResult
→ QwenService.chat()
→ RabbitMQWorker.process_job()
→ événement completed

APPELÉ PAR :
- apps/worker/src/local_qwen_worker/service.py
  ::QwenService.chat()

APPELLE :
- Client.chat() du paquet Python ollama
- Le serveur Ollama défini par OLLAMA_BASE_URL

NE CONNAÎT PAS :
- RabbitMQ ;
- FastAPI ;
- Django ;
- les identifiants de jobs ;
- les sessions du navigateur.

PIPELINES :
- STARTUP
- CHAT_JOB_GENERATE
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import logging
from collections.abc import (
    Mapping,
    Sequence,
)
from typing import Any, TypeAlias

from ollama import (
    Client,
    ResponseError,
)

from .config import (
    WorkerSettings,
    get_settings,
)
from .schemas import (
    ChatMessage,
    ChatResult,
)


# Logger propre aux communications avec Ollama.
#
# Il indique notamment :
# - le modèle appelé ;
# - le nombre de messages envoyés ;
# - la réception d'une réponse ;
# - les erreurs HTTP ou réseau.
logger = logging.getLogger(__name__)


# Représente la structure JSON transmise
# au client Python Ollama pour un message.
OllamaMessagePayload: TypeAlias = dict[str, str]


# Représente les paramètres numériques
# transmis à Ollama pendant la génération.
OllamaOptions: TypeAlias = dict[
    str,
    int | float,
]


# RÔLE :
# Classe de base pour toutes les erreurs prévues
# pendant la génération Qwen.
#
# LEVÉE PAR :
# - OllamaClient
#
# INTERCEPTÉE PAR :
# - apps/worker/src/local_qwen_worker/
#   rabbitmq_worker.py::RabbitMQWorker.process_job()
#
# CONSÉQUENCE :
# Le worker publie un événement failed
# dans la file RabbitMQ mycoder.events.
#
# PIPELINE :
# - CHAT_JOB_FAILURE
class QwenWorkerError(RuntimeError):
    """Erreur contrôlée pouvant être affichée dans le front."""


# RÔLE :
# Signale que le serveur Ollama ne peut pas
# être joint ou que la communication a été interrompue.
#
# LEVÉE PAR :
# - OllamaClient._request_chat()
#
# INTERCEPTÉE PAR :
# - RabbitMQWorker.process_job()
#
# EXEMPLES :
# - Ollama n'est pas démarré ;
# - mauvais port ;
# - connexion refusée ;
# - délai dépassé ;
# - interruption du serveur.
#
# PIPELINE :
# - CHAT_JOB_FAILURE
class OllamaUnavailableError(QwenWorkerError):
    """Ollama n'est pas joignable."""


# RÔLE :
# Signale qu'Ollama est joignable mais refuse
# la demande ou renvoie une réponse inutilisable.
#
# LEVÉE PAR :
# - OllamaClient._translate_response_error()
# - OllamaClient._extract_content()
# - OllamaClient._extract_model_name()
#
# EXEMPLES :
# - modèle absent ;
# - demande rejetée ;
# - réponse vide ;
# - format de réponse invalide.
#
# PIPELINE :
# - CHAT_JOB_FAILURE
class OllamaRequestError(QwenWorkerError):
    """Ollama a refusé la requête ou renvoyé une réponse invalide."""


# RÔLE :
# Encapsule le client officiel Ollama
# et expose une méthode simple chat().
#
# INSTANCIÉE PAR :
# - apps/worker/src/local_qwen_worker/
#   service.py::QwenService.__init__()
#
# UTILISÉE PAR :
# - QwenService.chat()
#
# RESPONSABILITÉS :
# - préparer le format attendu par Ollama ;
# - appliquer les paramètres du modèle ;
# - exécuter la requête ;
# - traduire les erreurs techniques ;
# - valider le contenu de la réponse ;
# - retourner un ChatResult.
#
# PIPELINES :
# - STARTUP
# - CHAT_JOB_GENERATE
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
class OllamaClient:
    # RÔLE :
    # Initialise la configuration et le client Ollama.
    #
    # APPELÉE PAR :
    # - QwenService.__init__()
    # - Tests unitaires
    #
    # APPELLE :
    # - apps/worker/src/local_qwen_worker/
    #   config.py::get_settings()
    # - ollama.Client()
    #
    # INJECTION :
    # Le paramètre client permet de fournir
    # un faux client pendant les tests.
    #
    # PIPELINE :
    # - STARTUP
    def __init__(
        self,
        settings: WorkerSettings | None = None,
        client: Client | None = None,
    ) -> None:
        self.settings = (
            settings
            if settings is not None
            else get_settings()
        )

        self._validate_settings()

        self.client = (
            client
            if client is not None
            else Client(
                host=self.settings.ollama_base_url,
                timeout=(
                    self.settings
                    .ollama_timeout_seconds
                ),
            )
        )

    # RÔLE :
    # Vérifie les paramètres indispensables
    # avant la création ou l'utilisation du client.
    #
    # APPELÉE PAR :
    # - __init__()
    #
    # VÉRIFIE :
    # - le nom du modèle ;
    # - l'adresse Ollama ;
    # - la taille du contexte ;
    # - le délai maximal.
    #
    # ERREUR :
    # - ValueError lorsqu'une configuration
    #   ne permet pas d'appeler Ollama.
    #
    # PIPELINE :
    # - STARTUP
    def _validate_settings(
        self,
    ) -> None:
        if not self.settings.qwen_model.strip():
            raise ValueError(
                "Le nom du modèle Qwen "
                "ne peut pas être vide."
            )

        if not self.settings.ollama_base_url.strip():
            raise ValueError(
                "L'adresse du serveur Ollama "
                "ne peut pas être vide."
            )

        if self.settings.qwen_context <= 0:
            raise ValueError(
                "La taille du contexte Qwen "
                "doit être supérieure à zéro."
            )

        if (
            self.settings.ollama_timeout_seconds
            <= 0
        ):
            raise ValueError(
                "Le délai Ollama doit être "
                "supérieur à zéro."
            )

    # RÔLE :
    # Envoie une conversation préparée
    # au modèle Qwen exécuté par Ollama.
    #
    # APPELÉE PAR :
    # - apps/worker/src/local_qwen_worker/
    #   service.py::QwenService.chat()
    #
    # APPELLE :
    # - _validate_messages()
    # - _serialise_messages()
    # - _build_options()
    # - _request_chat()
    # - _parse_response()
    #
    # REÇOIT :
    # - une séquence de ChatMessage ;
    # - le prompt système est déjà présent
    #   lorsqu'il doit être utilisé.
    #
    # RETOURNE :
    # - ChatResult avec le contenu généré
    #   et le nom du modèle.
    #
    # PIPELINES :
    # - CHAT_JOB_GENERATE
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    def chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> ChatResult:
        self._validate_messages(
            messages
        )

        payload_messages = (
            self._serialise_messages(
                messages
            )
        )

        options = self._build_options()

        logger.info(
            "Appel Ollama : modèle=%s, "
            "messages=%s, contexte=%s.",
            self.settings.qwen_model,
            len(payload_messages),
            self.settings.qwen_context,
        )

        response = self._request_chat(
            messages=payload_messages,
            options=options,
        )

        result = self._parse_response(
            response
        )

        logger.info(
            "Réponse Ollama reçue : "
            "modèle=%s, caractères=%s.",
            result.model,
            len(result.content),
        )

        return result

    # RÔLE :
    # Vérifie que la conversation reçue
    # peut être envoyée à Ollama.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # VÉRIFIE :
    # - présence d'au moins un message ;
    # - présence d'au moins un rôle user ;
    # - présence maximale d'un rôle system ;
    # - placement du rôle system en première position.
    #
    # ERREUR :
    # - ValueError si la conversation
    #   préparée est incohérente.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    @staticmethod
    def _validate_messages(
        messages: Sequence[ChatMessage],
    ) -> None:
        if not messages:
            raise ValueError(
                "Aucun message n'a été fourni "
                "au client Ollama."
            )

        if not any(
            message.role == "user"
            for message in messages
        ):
            raise ValueError(
                "La conversation Ollama doit contenir "
                "au moins un message utilisateur."
            )

        system_positions = [
            position
            for position, message in enumerate(
                messages
            )
            if message.role == "system"
        ]

        if len(system_positions) > 1:
            raise ValueError(
                "La conversation Ollama ne peut "
                "contenir qu'un message système."
            )

        if (
            system_positions
            and system_positions[0] != 0
        ):
            raise ValueError(
                "Le message système doit être "
                "le premier message envoyé à Ollama."
            )

    # RÔLE :
    # Transforme les ChatMessage Pydantic
    # en dictionnaires simples compris par Ollama.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # RETOURNE :
    # [
    #     {
    #         "role": "system",
    #         "content": "..."
    #     },
    #     {
    #         "role": "user",
    #         "content": "..."
    #     }
    # ]
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    @staticmethod
    def _serialise_messages(
        messages: Sequence[ChatMessage],
    ) -> list[OllamaMessagePayload]:
        return [
            {
                "role": message.role,
                "content": message.content,
            }
            for message in messages
        ]

    # RÔLE :
    # Construit les paramètres numériques
    # transmis au modèle pendant la génération.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # LIT :
    # - QWEN_TEMPERATURE
    # - QWEN_CONTEXT
    #
    # RETOURNE :
    # {
    #     "temperature": 0.25,
    #     "num_ctx": 8192
    # }
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    def _build_options(
        self,
    ) -> OllamaOptions:
        return {
            "temperature": (
                self.settings.qwen_temperature
            ),
            "num_ctx": (
                self.settings.qwen_context
            ),
        }

    # RÔLE :
    # Exécute l'appel réseau vers Ollama.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # APPELLE :
    # - ollama.Client.chat()
    # - _translate_response_error()
    #
    # PARAMÈTRES IMPORTANTS :
    # - stream=False :
    #   attend la réponse complète ;
    #
    # - keep_alive :
    #   conserve temporairement le modèle
    #   chargé en mémoire ;
    #
    # - options :
    #   température et taille du contexte.
    #
    # RETOURNE :
    # - La réponse brute du paquet ollama.
    #
    # ERREURS :
    # - ResponseError → OllamaRequestError ;
    # - autre exception → OllamaUnavailableError.
    #
    # PIPELINES :
    # - CHAT_JOB_GENERATE
    # - CHAT_JOB_FAILURE
    def _request_chat(
        self,
        messages: list[OllamaMessagePayload],
        options: OllamaOptions,
    ) -> Any:
        try:
            return self.client.chat(
                model=self.settings.qwen_model,
                messages=messages,
                stream=False,
                options=options,
                keep_alive=(
                    self.settings.ollama_keep_alive
                ),
            )

        except ResponseError as exc:
            raise self._translate_response_error(
                exc
            ) from exc

        except Exception as exc:
            logger.exception(
                "La communication avec Ollama "
                "a échoué."
            )

            raise OllamaUnavailableError(
                "Impossible de joindre Ollama. "
                "Vérifie que l'application est ouverte "
                "puis lance `make ollama-check`."
            ) from exc

    # RÔLE :
    # Transforme une erreur HTTP Ollama
    # en erreur métier compréhensible.
    #
    # APPELÉE PAR :
    # - _request_chat()
    #
    # RETOURNE :
    # - OllamaRequestError
    #
    # CAS PARTICULIER :
    # Une erreur HTTP 404 indique généralement
    # que le modèle demandé n'est pas installé.
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    def _translate_response_error(
        self,
        error: ResponseError,
    ) -> OllamaRequestError:
        status_code = getattr(
            error,
            "status_code",
            None,
        )

        if status_code == 404:
            return OllamaRequestError(
                "Le modèle "
                f"`{self.settings.qwen_model}` "
                "n'est pas installé dans Ollama. "
                "Lance `make model-pull`."
            )

        detail = self._extract_response_error_detail(
            error
        )

        return OllamaRequestError(
            "Ollama a refusé la requête"
            + (
                f" avec le statut HTTP {status_code}"
                if status_code is not None
                else ""
            )
            + f" : {detail}"
        )

    # RÔLE :
    # Extrait le texte détaillé contenu
    # dans une exception ResponseError.
    #
    # APPELÉE PAR :
    # - _translate_response_error()
    #
    # RETOURNE :
    # - error.error lorsqu'il est exploitable ;
    # - la représentation texte de l'exception sinon.
    #
    # PIPELINE :
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _extract_response_error_detail(
        error: ResponseError,
    ) -> str:
        raw_detail = getattr(
            error,
            "error",
            None,
        )

        if (
            isinstance(raw_detail, str)
            and raw_detail.strip()
        ):
            return raw_detail.strip()

        fallback_detail = str(error).strip()

        return (
            fallback_detail
            or "Erreur Ollama sans détail."
        )

    # RÔLE :
    # Transforme la réponse brute d'Ollama
    # en ChatResult validé.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # APPELLE :
    # - _extract_content()
    # - _extract_model_name()
    #
    # RETOURNE :
    # - ChatResult
    #
    # PIPELINES :
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    def _parse_response(
        self,
        response: Any,
    ) -> ChatResult:
        content = self._extract_content(
            response
        )

        model_name = self._extract_model_name(
            response
        )

        return ChatResult(
            content=content,
            model=model_name,
        )

    # RÔLE :
    # Extrait le texte généré depuis
    # une réponse objet ou dictionnaire.
    #
    # APPELÉE PAR :
    # - _parse_response()
    #
    # FORMATS ACCEPTÉS :
    # - response.message.content ;
    # - response["message"]["content"].
    #
    # RETOURNE :
    # - Le contenu nettoyé.
    #
    # ERREUR :
    # - OllamaRequestError si le contenu
    #   est absent ou vide.
    #
    # PIPELINES :
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    @staticmethod
    def _extract_content(
        response: Any,
    ) -> str:
        message = getattr(
            response,
            "message",
            None,
        )

        if (
            message is None
            and isinstance(response, Mapping)
        ):
            message = response.get("message")

        if isinstance(message, Mapping):
            raw_content = message.get("content")

        else:
            raw_content = getattr(
                message,
                "content",
                None,
            )

        if not isinstance(raw_content, str):
            raise OllamaRequestError(
                "Ollama a retourné une réponse "
                "sans contenu textuel valide."
            )

        content = raw_content.strip()

        if not content:
            raise OllamaRequestError(
                "Ollama a retourné une réponse vide."
            )

        return content

    # RÔLE :
    # Extrait le nom du modèle réellement
    # indiqué par Ollama.
    #
    # APPELÉE PAR :
    # - _parse_response()
    #
    # FORMATS ACCEPTÉS :
    # - response.model ;
    # - response["model"].
    #
    # VALEUR DE REPLI :
    # - QWEN_MODEL défini dans WorkerSettings.
    #
    # RETOURNE :
    # - Un nom de modèle non vide.
    #
    # PIPELINE :
    # - CHAT_JOB_COMPLETE
    def _extract_model_name(
        self,
        response: Any,
    ) -> str:
        raw_model = getattr(
            response,
            "model",
            None,
        )

        if (
            raw_model is None
            and isinstance(response, Mapping)
        ):
            raw_model = response.get("model")

        if isinstance(raw_model, str):
            model_name = raw_model.strip()

            if model_name:
                return model_name

        fallback_model = (
            self.settings.qwen_model.strip()
        )

        if not fallback_model:
            raise OllamaRequestError(
                "Ollama n'a pas indiqué le modèle "
                "utilisé et aucun modèle de repli "
                "n'est configuré."
            )

        return fallback_model