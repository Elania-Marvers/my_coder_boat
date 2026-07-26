"""
FICHIER :
apps/worker/src/local_qwen_worker/service.py

RÔLE GÉNÉRAL :
Prépare une conversation avant de la transmettre
au client Ollama.

QwenService est volontairement indépendant de RabbitMQ.

Il ne connaît pas :

- les files mycoder.jobs et mycoder.events ;
- les identifiants des jobs ;
- FastAPI ;
- Django ;
- le navigateur.

Il reçoit uniquement une conversation, la valide,
ajoute éventuellement le prompt système puis appelle
OllamaClient.

CIRCULATION :
RabbitMQWorker._generate_response()
→ QwenService.chat()
→ QwenService._validate_messages()
→ QwenService._prepare_messages()
→ OllamaClient.chat()
→ Ollama
→ ChatResult

APPELÉ PAR :
- apps/worker/src/local_qwen_worker/rabbitmq_worker.py
  ::RabbitMQWorker._generate_response()

APPELLE :
- apps/worker/src/local_qwen_worker/ollama_client.py
  ::OllamaClient.chat()

PIPELINES :
- CHAT_JOB_GENERATE
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import logging
from collections.abc import (
    Mapping,
    Sequence,
)
from typing import Final, TypeAlias

from .ollama_client import OllamaClient
from .schemas import (
    ChatMessage,
    ChatResult,
)


# Logger propre au service Qwen.
logger = logging.getLogger(__name__)


# Nombre maximal de messages reçus
# avant l'ajout éventuel du prompt système.
#
# Cette valeur doit rester cohérente avec :
#
# apps/api/app/schemas.py::MAX_HISTORY_MESSAGES
# apps/worker/src/local_qwen_worker/
# rabbitmq_worker.py::MAX_HISTORY_MESSAGES
MAX_CONVERSATION_MESSAGES: Final[int] = 20


# Nombre maximal total après ajout
# du prompt système par le service.
MAX_PREPARED_MESSAGES: Final[int] = (
    MAX_CONVERSATION_MESSAGES + 1
)


# Rôle utilisé par le message système.
SYSTEM_ROLE: Final[str] = "system"


# Type accepté par QwenService.
#
# Un appelant peut fournir :
#
# - un ChatMessage déjà validé ;
# - un dictionnaire contenant role et content.
MessageInput: TypeAlias = (
    ChatMessage
    | Mapping[str, str]
)


# RÔLE :
# Prépare les conversations puis délègue
# leur génération au client Ollama.
#
# INSTANCIÉE PAR :
# - apps/worker/src/local_qwen_worker/
#   rabbitmq_worker.py::RabbitMQWorker.__init__()
#
# UTILISÉE PAR :
# - RabbitMQWorker._generate_response()
#
# APPELLE :
# - OllamaClient.chat()
#
# PIPELINE :
# - CHAT_JOB_GENERATE
class QwenService:
    # RÔLE :
    # Initialise le service avec un client Ollama
    # fourni ou avec un nouveau client par défaut.
    #
    # APPELÉE PAR :
    # - RabbitMQWorker.__init__()
    # - Tests unitaires
    #
    # APPELLE :
    # - OllamaClient()
    #
    # INJECTION :
    # Le paramètre client permet de fournir
    # un faux client dans les tests sans lancer Ollama.
    #
    # PIPELINES :
    # - STARTUP
    # - CHAT_JOB_GENERATE
    def __init__(
        self,
        client: OllamaClient | None = None,
    ) -> None:
        self.client = (
            client
            if client is not None
            else OllamaClient()
        )

    # RÔLE :
    # Prépare une conversation complète
    # puis demande sa génération à Ollama.
    #
    # APPELÉE PAR :
    # - apps/worker/src/local_qwen_worker/
    #   rabbitmq_worker.py
    #   ::RabbitMQWorker._generate_response()
    #
    # APPELLE :
    # - _validate_messages()
    # - _normalise_system_prompt()
    # - _prepare_messages()
    # - _validate_prepared_messages()
    # - OllamaClient.chat()
    #
    # REÇOIT :
    # - messages :
    #   conversation user/assistant validée ;
    #
    # - system_prompt :
    #   instructions générales facultatives.
    #
    # RETOURNE :
    # - ChatResult contenant :
    #   - content ;
    #   - model.
    #
    # ERREURS :
    # - ValueError pour une conversation invalide ;
    # - QwenWorkerError provenant d'OllamaClient.
    #
    # PIPELINES :
    # - CHAT_JOB_GENERATE
    # - CHAT_JOB_COMPLETE
    # - CHAT_JOB_FAILURE
    def chat(
        self,
        messages: Sequence[MessageInput],
        system_prompt: str | None = None,
    ) -> ChatResult:
        logger.info(
            "Préparation d'une conversation "
            "de %s message(s).",
            len(messages),
        )

        validated_messages = (
            self._validate_messages(
                messages
            )
        )

        clean_system_prompt = (
            self._normalise_system_prompt(
                system_prompt
            )
        )

        prepared_messages = (
            self._prepare_messages(
                messages=validated_messages,
                system_prompt=clean_system_prompt,
            )
        )

        self._validate_prepared_messages(
            prepared_messages
        )

        logger.info(
            "Envoi de %s message(s) "
            "au client Ollama.",
            len(prepared_messages),
        )

        result = self.client.chat(
            prepared_messages
        )

        logger.info(
            "Réponse reçue depuis le modèle %s.",
            result.model,
        )

        return result

    # RÔLE :
    # Valide tous les messages reçus par le service.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # APPELLE :
    # - _validate_message()
    #
    # VÉRIFIE :
    # - que la conversation n'est pas vide ;
    # - qu'elle ne dépasse pas vingt messages ;
    # - que chaque élément devient un ChatMessage.
    #
    # RETOURNE :
    # - Une nouvelle liste de ChatMessage.
    #
    # ERREURS :
    # - ValueError lorsque la conversation
    #   est vide ou trop longue ;
    # - ValidationError Pydantic indirecte
    #   lorsqu'un message est invalide.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    def _validate_messages(
        self,
        messages: Sequence[MessageInput],
    ) -> list[ChatMessage]:
        if not messages:
            raise ValueError(
                "La conversation ne peut pas être vide."
            )

        if (
            len(messages)
            > MAX_CONVERSATION_MESSAGES
        ):
            raise ValueError(
                "La conversation dépasse la limite "
                f"de {MAX_CONVERSATION_MESSAGES} messages."
            )

        return [
            self._validate_message(message)
            for message in messages
        ]

    # RÔLE :
    # Valide un message individuel.
    #
    # APPELÉE PAR :
    # - _validate_messages()
    #
    # APPELLE :
    # - ChatMessage.model_validate()
    #
    # COMPORTEMENT :
    # - retourne directement un ChatMessage existant ;
    # - transforme un dictionnaire en ChatMessage ;
    # - crée une copie afin d'éviter qu'un appelant
    #   modifie ensuite l'objet utilisé par le service.
    #
    # RETOURNE :
    # - ChatMessage validé.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    @staticmethod
    def _validate_message(
        message: MessageInput,
    ) -> ChatMessage:
        if isinstance(message, ChatMessage):
            return message.model_copy(
                deep=True
            )

        return ChatMessage.model_validate(
            message
        )

    # RÔLE :
    # Nettoie le prompt système facultatif.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # RETOURNE :
    # - Le texte nettoyé ;
    # - None lorsque le prompt est absent ou vide.
    #
    # IMPORTANT :
    # Le prompt système utilisé par le worker
    # provient normalement de :
    #
    # apps/worker/src/local_qwen_worker/prompts.py
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    @staticmethod
    def _normalise_system_prompt(
        system_prompt: str | None,
    ) -> str | None:
        if system_prompt is None:
            return None

        clean_prompt = system_prompt.strip()

        if not clean_prompt:
            return None

        return clean_prompt

    # RÔLE :
    # Construit la conversation finale envoyée
    # au modèle Ollama.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # APPELLE :
    # - _contains_system_message()
    # - _prepend_system_prompt()
    #
    # COMPORTEMENT :
    # - conserve la conversation validée ;
    # - ajoute le prompt système lorsqu'il existe ;
    # - n'ajoute pas un second message système.
    #
    # RETOURNE :
    # - Liste complète de ChatMessage.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    def _prepare_messages(
        self,
        messages: Sequence[ChatMessage],
        system_prompt: str | None,
    ) -> list[ChatMessage]:
        prepared_messages = [
            message.model_copy(deep=True)
            for message in messages
        ]

        if system_prompt is None:
            return prepared_messages

        if self._contains_system_message(
            prepared_messages
        ):
            logger.warning(
                "La conversation contient déjà "
                "un message système : le prompt "
                "fourni ne sera pas ajouté."
            )

            return prepared_messages

        return self._prepend_system_prompt(
            messages=prepared_messages,
            system_prompt=system_prompt,
        )

    # RÔLE :
    # Indique si une conversation contient
    # déjà un message système.
    #
    # APPELÉE PAR :
    # - _prepare_messages()
    #
    # RETOURNE :
    # - True lorsqu'un rôle system est trouvé ;
    # - False sinon.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    @staticmethod
    def _contains_system_message(
        messages: Sequence[ChatMessage],
    ) -> bool:
        return any(
            message.role == SYSTEM_ROLE
            for message in messages
        )

    # RÔLE :
    # Ajoute un message système au début
    # de la conversation.
    #
    # APPELÉE PAR :
    # - _prepare_messages()
    #
    # CONSTRUIT :
    # - ChatMessage(
    #       role="system",
    #       content=system_prompt,
    #   )
    #
    # RETOURNE :
    # - Une nouvelle liste commençant
    #   par le prompt système.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    @staticmethod
    def _prepend_system_prompt(
        messages: Sequence[ChatMessage],
        system_prompt: str,
    ) -> list[ChatMessage]:
        system_message = ChatMessage(
            role="system",
            content=system_prompt,
        )

        return [
            system_message,
            *messages,
        ]

    # RÔLE :
    # Vérifie la conversation finale juste
    # avant son envoi au client Ollama.
    #
    # APPELÉE PAR :
    # - chat()
    #
    # VÉRIFIE :
    # - que la liste n'est pas vide ;
    # - qu'elle ne dépasse pas vingt-et-un messages ;
    # - qu'elle contient un message utilisateur ;
    # - qu'elle ne contient qu'un message système ;
    # - que le message système est placé en premier.
    #
    # ERREURS :
    # - ValueError lorsque la conversation
    #   préparée est incohérente.
    #
    # PIPELINE :
    # - CHAT_JOB_GENERATE
    def _validate_prepared_messages(
        self,
        messages: Sequence[ChatMessage],
    ) -> None:
        if not messages:
            raise ValueError(
                "La conversation préparée "
                "ne peut pas être vide."
            )

        if len(messages) > MAX_PREPARED_MESSAGES:
            raise ValueError(
                "La conversation préparée dépasse "
                f"la limite de {MAX_PREPARED_MESSAGES} "
                "messages."
            )

        if not any(
            message.role == "user"
            for message in messages
        ):
            raise ValueError(
                "La conversation doit contenir "
                "au moins un message utilisateur."
            )

        system_positions = [
            position
            for position, message in enumerate(
                messages
            )
            if message.role == SYSTEM_ROLE
        ]

        if len(system_positions) > 1:
            raise ValueError(
                "La conversation ne peut contenir "
                "qu'un seul message système."
            )

        if (
            system_positions
            and system_positions[0] != 0
        ):
            raise ValueError(
                "Le message système doit être placé "
                "au début de la conversation."
            )