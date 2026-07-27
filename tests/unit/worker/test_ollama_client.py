"""
FICHIER :
tests/unit/worker/test_ollama_client.py

RÔLE GÉNÉRAL :
Teste le client chargé de communiquer directement
avec le serveur Ollama.

Aucun serveur Ollama réel n'est démarré.

Les tests utilisent de faux clients afin de contrôler :

- les paramètres transmis à Ollama ;
- le modèle demandé ;
- l'ordre des messages ;
- les options de génération ;
- l'analyse des réponses ;
- les erreurs HTTP ;
- les erreurs réseau ;
- les conversations invalides.

CIRCULATION TESTÉE :

QwenService
→ OllamaClient.chat()
→ faux client Ollama
→ réponse simulée
→ ChatResult

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_JOB_GENERATE
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from types import SimpleNamespace
from typing import Any

import pytest
from ollama import ResponseError

from local_qwen_worker.config import (
    WorkerSettings,
)
from local_qwen_worker.ollama_client import (
    OllamaClient,
    OllamaRequestError,
    OllamaUnavailableError,
)
from local_qwen_worker.schemas import (
    ChatMessage,
    ChatResult,
)


# RÔLE :
# Simule le client officiel Ollama.
#
# Cette classe :
#
# - conserve tous les appels reçus ;
# - retourne une réponse configurée ;
# - peut lever une exception configurée ;
# - n'effectue aucun appel réseau.
class RecordingOllamaClient:
    # RÔLE :
    # Initialise le faux client.
    #
    # REÇOIT :
    # - response :
    #   réponse retournée par chat() ;
    #
    # - error :
    #   exception éventuellement levée par chat().
    def __init__(
        self,
        *,
        response: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error

        self.calls: list[
            dict[str, Any]
        ] = []

    # RÔLE :
    # Simule ollama.Client.chat().
    #
    # MODIFIE :
    # - self.calls
    #
    # RETOURNE :
    # - la réponse configurée.
    #
    # ERREUR :
    # - lève self.error lorsqu'elle existe.
    def chat(
        self,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(kwargs)

        if self.error is not None:
            raise self.error

        return self.response


# RÔLE :
# Construit une erreur ResponseError
# compatible avec différentes versions
# du paquet Python ollama.
#
# APPELÉE PAR :
# - les tests des erreurs HTTP.
#
# RETOURNE :
# - ResponseError contenant un statut HTTP.
def _build_response_error(
    message: str,
    status_code: int,
) -> ResponseError:
    try:
        return ResponseError(
            error=message,
            status_code=status_code,
        )

    except TypeError:
        try:
            return ResponseError(
                message,
                status_code,
            )

        except TypeError:
            error = ResponseError(message)

            error.status_code = status_code

            return error


# RÔLE :
# Fournit une configuration worker contrôlée.
#
# Les valeurs sont volontairement différentes
# des valeurs par défaut afin de vérifier
# qu'OllamaClient les utilise réellement.
@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        qwen_model="qwen-test-model",
        qwen_context=4096,
        qwen_temperature=0.15,
        ollama_base_url=(
            "http://127.0.0.1:11434/"
        ),
        ollama_timeout_seconds=42.0,
        ollama_keep_alive="5m",
        rabbitmq_url=(
            "amqp://guest:guest@127.0.0.1:5672/"
        ),
        rabbitmq_job_queue="test.jobs",
        rabbitmq_event_queue="test.events",
    )


# RÔLE :
# Construit une conversation valide
# contenant un prompt système et une question.
#
# RETOURNE :
# - liste de ChatMessage.
@pytest.fixture
def valid_messages() -> list[ChatMessage]:
    return [
        ChatMessage(
            role="system",
            content=(
                "Tu es un assistant de test."
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Construction du client officiel
# ---------------------------------------------------------------------------


# Vérifie que l'absence de client injecté
# provoque la création du client officiel
# avec l'adresse et le délai configurés.
def test_constructor_builds_official_client(
    monkeypatch: pytest.MonkeyPatch,
    worker_settings: WorkerSettings,
) -> None:
    captured_arguments: dict[
        str,
        Any,
    ] = {}

    class FakeOfficialClient:
        # RÔLE :
        # Enregistre les arguments normalement
        # transmis à ollama.Client().
        def __init__(
            self,
            *,
            host: str,
            timeout: float,
        ) -> None:
            captured_arguments["host"] = host
            captured_arguments["timeout"] = timeout

    monkeypatch.setattr(
        (
            "local_qwen_worker."
            "ollama_client.Client"
        ),
        FakeOfficialClient,
    )

    client = OllamaClient(
        settings=worker_settings,
    )

    assert captured_arguments == {
        "host": "http://127.0.0.1:11434",
        "timeout": 42.0,
    }

    assert isinstance(
        client.client,
        FakeOfficialClient,
    )


# ---------------------------------------------------------------------------
# Construction de la requête Ollama
# ---------------------------------------------------------------------------


# Vérifie tous les paramètres transmis
# au client officiel Ollama.
def test_chat_sends_expected_request(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    fake_response = {
        "message": {
            "content": "Réponse simulée.",
        },
        "model": "returned-test-model",
    }

    fake_client = RecordingOllamaClient(
        response=fake_response,
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    result = client.chat(
        valid_messages
    )

    assert len(fake_client.calls) == 1

    request = fake_client.calls[0]

    assert request["model"] == (
        "qwen-test-model"
    )

    assert request["stream"] is False

    assert request["keep_alive"] == "5m"

    assert request["options"] == {
        "temperature": 0.15,
        "num_ctx": 4096,
    }

    assert request["messages"] == [
        {
            "role": "system",
            "content": (
                "Tu es un assistant de test."
            ),
        },
        {
            "role": "user",
            "content": (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        },
    ]

    assert result == ChatResult(
        content="Réponse simulée.",
        model="returned-test-model",
    )


# Vérifie que l'ordre des messages
# est conservé pendant la sérialisation.
def test_chat_preserves_message_order(
    worker_settings: WorkerSettings,
) -> None:
    fake_client = RecordingOllamaClient(
        response={
            "message": {
                "content": "Réponse.",
            },
            "model": "qwen-test-model",
        }
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    client.chat(
        [
            ChatMessage(
                role="user",
                content="Première question.",
            ),
            ChatMessage(
                role="assistant",
                content="Première réponse.",
            ),
            ChatMessage(
                role="user",
                content="Seconde question.",
            ),
        ]
    )

    request_messages = (
        fake_client
        .calls[0]["messages"]
    )

    assert [
        message["role"]
        for message in request_messages
    ] == [
        "user",
        "assistant",
        "user",
    ]


# ---------------------------------------------------------------------------
# Analyse des réponses
# ---------------------------------------------------------------------------


# Vérifie qu'une réponse constituée
# d'objets Python est correctement analysée.
def test_chat_parses_object_response(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    response = SimpleNamespace(
        message=SimpleNamespace(
            content="  Réponse objet.  ",
        ),
        model="  object-model  ",
    )

    fake_client = RecordingOllamaClient(
        response=response,
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    result = client.chat(
        valid_messages
    )

    assert result.content == "Réponse objet."
    assert result.model == "object-model"


# Vérifie qu'une réponse représentée
# par un dictionnaire est acceptée.
def test_chat_parses_mapping_response(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    response = {
        "message": {
            "content": (
                "  Réponse dictionnaire.  "
            ),
        },
        "model": "  mapping-model  ",
    }

    fake_client = RecordingOllamaClient(
        response=response,
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    result = client.chat(
        valid_messages
    )

    assert result.content == (
        "Réponse dictionnaire."
    )

    assert result.model == "mapping-model"


# Vérifie que le modèle configuré
# est utilisé lorsque la réponse Ollama
# n'indique aucun nom de modèle.
def test_chat_uses_configured_model_as_fallback(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    response = {
        "message": {
            "content": "Réponse valide.",
        }
    }

    fake_client = RecordingOllamaClient(
        response=response,
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    result = client.chat(
        valid_messages
    )

    assert result.model == "qwen-test-model"


# Vérifie qu'un contenu composé uniquement
# d'espaces est refusé.
def test_chat_rejects_blank_response_content(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    fake_client = RecordingOllamaClient(
        response={
            "message": {
                "content": "   ",
            },
            "model": "qwen-test-model",
        }
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        OllamaRequestError,
        match="réponse vide",
    ):
        client.chat(valid_messages)


# Vérifie qu'une réponse sans propriété
# message.content est refusée.
def test_chat_rejects_missing_response_content(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    fake_client = RecordingOllamaClient(
        response={
            "model": "qwen-test-model",
        }
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        OllamaRequestError,
        match="sans contenu textuel valide",
    ):
        client.chat(valid_messages)


# ---------------------------------------------------------------------------
# Traduction des erreurs Ollama
# ---------------------------------------------------------------------------


# Vérifie qu'une erreur HTTP 404
# est présentée comme un modèle absent.
def test_chat_translates_model_not_found_error(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    response_error = _build_response_error(
        "model not found",
        404,
    )

    fake_client = RecordingOllamaClient(
        error=response_error,
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        OllamaRequestError,
        match="qwen-test-model",
    ) as captured_error:
        client.chat(valid_messages)

    assert "make model-pull" in str(
        captured_error.value
    )


# Vérifie qu'une autre erreur HTTP
# conserve le statut et le détail Ollama.
def test_chat_translates_generic_response_error(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    response_error = _build_response_error(
        "invalid request",
        400,
    )

    fake_client = RecordingOllamaClient(
        error=response_error,
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        OllamaRequestError,
    ) as captured_error:
        client.chat(valid_messages)

    error_message = str(
        captured_error.value
    )

    assert "400" in error_message
    assert "invalid request" in error_message


# Vérifie qu'une erreur réseau
# devient OllamaUnavailableError.
def test_chat_translates_network_error(
    worker_settings: WorkerSettings,
    valid_messages: list[ChatMessage],
) -> None:
    fake_client = RecordingOllamaClient(
        error=ConnectionError(
            "Connexion refusée."
        )
    )

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        OllamaUnavailableError,
        match="Impossible de joindre Ollama",
    ):
        client.chat(valid_messages)


# ---------------------------------------------------------------------------
# Validation de la conversation
# ---------------------------------------------------------------------------


# Vérifie qu'une conversation vide
# est refusée avant l'appel réseau.
def test_chat_rejects_empty_messages(
    worker_settings: WorkerSettings,
) -> None:
    fake_client = RecordingOllamaClient()

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        ValueError,
        match="Aucun message",
    ):
        client.chat([])

    assert fake_client.calls == []


# Vérifie qu'au moins un message utilisateur
# doit être présent.
def test_chat_rejects_conversation_without_user(
    worker_settings: WorkerSettings,
) -> None:
    fake_client = RecordingOllamaClient()

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        ValueError,
        match="au moins un message utilisateur",
    ):
        client.chat(
            [
                ChatMessage(
                    role="assistant",
                    content="Réponse isolée.",
                )
            ]
        )

    assert fake_client.calls == []


# Vérifie qu'une conversation
# ne peut pas contenir deux prompts système.
def test_chat_rejects_multiple_system_messages(
    worker_settings: WorkerSettings,
) -> None:
    fake_client = RecordingOllamaClient()

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        ValueError,
        match="qu'un message système",
    ):
        client.chat(
            [
                ChatMessage(
                    role="system",
                    content="Premier prompt.",
                ),
                ChatMessage(
                    role="system",
                    content="Second prompt.",
                ),
                ChatMessage(
                    role="user",
                    content="Bonjour.",
                ),
            ]
        )

    assert fake_client.calls == []


# Vérifie que le prompt système
# doit être le premier message.
def test_chat_rejects_misplaced_system_message(
    worker_settings: WorkerSettings,
) -> None:
    fake_client = RecordingOllamaClient()

    client = OllamaClient(
        settings=worker_settings,
        client=fake_client,
    )

    with pytest.raises(
        ValueError,
        match="premier message",
    ):
        client.chat(
            [
                ChatMessage(
                    role="user",
                    content="Bonjour.",
                ),
                ChatMessage(
                    role="system",
                    content="Prompt mal placé.",
                ),
            ]
        )

    assert fake_client.calls == []