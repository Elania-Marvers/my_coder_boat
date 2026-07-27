"""
FICHIER :
tests/unit/worker/test_service.py

RÔLE GÉNÉRAL :
Teste la préparation des conversations effectuée
par QwenService avant leur transmission à OllamaClient.

Ces tests n'appellent pas réellement Ollama.

Un faux client remplace OllamaClient afin de vérifier :

- les messages reçus ;
- leur ordre ;
- l'ajout du prompt système ;
- la valeur retournée par le service.

CIRCULATION TESTÉE :

RabbitMQWorker
→ QwenService.chat()
→ préparation des messages
→ faux client Ollama
→ ChatResult

ÉLÉMENTS TESTÉS :

- conversation vide ;
- limite de vingt messages ;
- conversion des dictionnaires ;
- ajout du prompt système ;
- prompt système vide ;
- message système déjà présent ;
- présence obligatoire d'un utilisateur ;
- placement du message système ;
- absence de doublon système ;
- transmission du résultat Ollama.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_JOB_GENERATE
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from local_qwen_worker.schemas import (
    ChatMessage,
    ChatResult,
)
from local_qwen_worker.service import (
    MAX_CONVERSATION_MESSAGES,
    QwenService,
)


# RÔLE :
# Remplace OllamaClient pendant les tests.
#
# Cette classe :
#
# - conserve les messages reçus ;
# - ne réalise aucun appel réseau ;
# - retourne toujours un ChatResult connu.
#
# UTILISÉE PAR :
# - la fixture fake_client()
# - les tests de QwenService.chat()
class FakeOllamaClient:
    # RÔLE :
    # Prépare le faux résultat et le stockage
    # des messages reçus.
    def __init__(self) -> None:
        self.received_messages: (
            list[ChatMessage]
            | None
        ) = None

        self.result = ChatResult(
            content="Réponse générée par le faux client.",
            model="fake-qwen-model",
        )

    # RÔLE :
    # Simule OllamaClient.chat().
    #
    # APPELÉE PAR :
    # - QwenService.chat()
    #
    # MODIFIE :
    # - self.received_messages
    #
    # RETOURNE :
    # - ChatResult fixe.
    def chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> ChatResult:
        self.received_messages = [
            message.model_copy(deep=True)
            for message in messages
        ]

        return self.result


# RÔLE :
# Simule un client susceptible de modifier
# les objets reçus.
#
# Le test associé vérifie que QwenService
# transmet des copies des messages d'origine.
class MutatingFakeOllamaClient:
    # RÔLE :
    # Modifie volontairement le premier message
    # reçu avant de retourner un résultat.
    def chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> ChatResult:
        messages[0].content = (
            "Contenu modifié par le faux client."
        )

        return ChatResult(
            content="Réponse après modification.",
            model="mutating-fake-model",
        )


# RÔLE :
# Fournit un faux client neuf à chaque test.
#
# RETOURNE :
# - FakeOllamaClient
@pytest.fixture
def fake_client() -> FakeOllamaClient:
    return FakeOllamaClient()


# RÔLE :
# Fournit un QwenService utilisant le faux client.
#
# APPELLE :
# - QwenService.__init__()
#
# RETOURNE :
# - QwenService indépendant d'Ollama.
@pytest.fixture
def service(
    fake_client: FakeOllamaClient,
) -> QwenService:
    return QwenService(
        client=fake_client,
    )


# ---------------------------------------------------------------------------
# Tests de l'appel principal
# ---------------------------------------------------------------------------


# Vérifie que QwenService retourne exactement
# le résultat produit par le client Ollama.
def test_chat_returns_client_result(
    service: QwenService,
) -> None:
    result = service.chat(
        [
            {
                "role": "user",
                "content": "Bonjour.",
            }
        ]
    )

    assert result.content == (
        "Réponse générée par le faux client."
    )

    assert result.model == "fake-qwen-model"


# Vérifie que les dictionnaires reçus
# sont convertis en objets ChatMessage.
def test_chat_converts_mapping_to_chat_message(
    service: QwenService,
    fake_client: FakeOllamaClient,
) -> None:
    service.chat(
        [
            {
                "role": "user",
                "content": "Explique les frégates.",
            }
        ]
    )

    assert fake_client.received_messages is not None
    assert len(fake_client.received_messages) == 1

    received_message = (
        fake_client.received_messages[0]
    )

    assert isinstance(
        received_message,
        ChatMessage,
    )

    assert received_message.role == "user"

    assert received_message.content == (
        "Explique les frégates."
    )


# Vérifie qu'une conversation valide
# peut être envoyée sans prompt système.
def test_chat_accepts_conversation_without_system_prompt(
    service: QwenService,
    fake_client: FakeOllamaClient,
) -> None:
    service.chat(
        [
            ChatMessage(
                role="user",
                content="Bonjour.",
            )
        ]
    )

    assert fake_client.received_messages is not None

    assert [
        message.role
        for message
        in fake_client.received_messages
    ] == [
        "user",
    ]


# ---------------------------------------------------------------------------
# Tests du prompt système
# ---------------------------------------------------------------------------


# Vérifie que le prompt système est ajouté
# au début de la conversation.
def test_chat_prepends_system_prompt(
    service: QwenService,
    fake_client: FakeOllamaClient,
) -> None:
    service.chat(
        [
            {
                "role": "user",
                "content": "Bonjour.",
            }
        ],
        system_prompt=(
            "Tu es un assistant maritime."
        ),
    )

    assert fake_client.received_messages is not None

    assert len(
        fake_client.received_messages
    ) == 2

    system_message = (
        fake_client.received_messages[0]
    )

    user_message = (
        fake_client.received_messages[1]
    )

    assert system_message.role == "system"

    assert system_message.content == (
        "Tu es un assistant maritime."
    )

    assert user_message.role == "user"
    assert user_message.content == "Bonjour."


# Vérifie que les espaces entourant
# le prompt système sont supprimés.
def test_chat_strips_system_prompt(
    service: QwenService,
    fake_client: FakeOllamaClient,
) -> None:
    service.chat(
        [
            {
                "role": "user",
                "content": "Bonjour.",
            }
        ],
        system_prompt=(
            "   Tu es un assistant local.   "
        ),
    )

    assert fake_client.received_messages is not None

    assert (
        fake_client
        .received_messages[0]
        .content
    ) == "Tu es un assistant local."


# Vérifie qu'un prompt système vide
# n'ajoute aucun message.
def test_chat_ignores_blank_system_prompt(
    service: QwenService,
    fake_client: FakeOllamaClient,
) -> None:
    service.chat(
        [
            {
                "role": "user",
                "content": "Bonjour.",
            }
        ],
        system_prompt="   ",
    )

    assert fake_client.received_messages is not None

    assert len(
        fake_client.received_messages
    ) == 1

    assert (
        fake_client
        .received_messages[0]
        .role
    ) == "user"


# Vérifie qu'un second message système
# n'est pas ajouté lorsqu'il en existe déjà un.
def test_chat_does_not_duplicate_existing_system_message(
    service: QwenService,
    fake_client: FakeOllamaClient,
) -> None:
    service.chat(
        [
            ChatMessage(
                role="system",
                content="Prompt déjà présent.",
            ),
            ChatMessage(
                role="user",
                content="Bonjour.",
            ),
        ],
        system_prompt=(
            "Nouveau prompt qui doit être ignoré."
        ),
    )

    assert fake_client.received_messages is not None

    system_messages = [
        message
        for message
        in fake_client.received_messages
        if message.role == "system"
    ]

    assert len(system_messages) == 1

    assert system_messages[0].content == (
        "Prompt déjà présent."
    )


# ---------------------------------------------------------------------------
# Tests des copies de messages
# ---------------------------------------------------------------------------


# Vérifie que le service protège les objets
# fournis par l'appelant.
#
# Le faux client modifie volontairement le message
# qu'il reçoit. Le message original doit rester intact.
def test_chat_does_not_mutate_original_message() -> None:
    original_message = ChatMessage(
        role="user",
        content="Message original.",
    )

    mutating_service = QwenService(
        client=MutatingFakeOllamaClient(),
    )

    mutating_service.chat(
        [
            original_message,
        ]
    )

    assert original_message.content == (
        "Message original."
    )


# ---------------------------------------------------------------------------
# Tests des conversations invalides
# ---------------------------------------------------------------------------


# Vérifie qu'une conversation vide
# est refusée avant l'appel du client.
def test_chat_rejects_empty_conversation(
    service: QwenService,
) -> None:
    with pytest.raises(
        ValueError,
        match="ne peut pas être vide",
    ):
        service.chat([])


# Vérifie que la limite de vingt messages
# est appliquée par le service.
def test_chat_rejects_more_than_twenty_messages(
    service: QwenService,
) -> None:
    messages = [
        {
            "role": "user",
            "content": f"Message numéro {index}.",
        }
        for index in range(
            MAX_CONVERSATION_MESSAGES + 1
        )
    ]

    with pytest.raises(
        ValueError,
        match="dépasse la limite",
    ):
        service.chat(messages)


# Vérifie qu'une conversation sans aucun
# message utilisateur est refusée.
def test_chat_rejects_conversation_without_user(
    service: QwenService,
) -> None:
    with pytest.raises(
        ValueError,
        match=(
            "au moins un message utilisateur"
        ),
    ):
        service.chat(
            [
                {
                    "role": "assistant",
                    "content": (
                        "Réponse sans question."
                    ),
                }
            ]
        )


# Vérifie qu'une conversation ne peut pas
# contenir deux messages système.
def test_chat_rejects_multiple_system_messages(
    service: QwenService,
) -> None:
    with pytest.raises(
        ValueError,
        match=(
            "qu'un seul message système"
        ),
    ):
        service.chat(
            [
                {
                    "role": "system",
                    "content": "Premier prompt.",
                },
                {
                    "role": "system",
                    "content": "Second prompt.",
                },
                {
                    "role": "user",
                    "content": "Bonjour.",
                },
            ]
        )


# Vérifie que le message système doit
# occuper la première position.
def test_chat_rejects_system_message_after_user(
    service: QwenService,
) -> None:
    with pytest.raises(
        ValueError,
        match=(
            "placé au début"
        ),
    ):
        service.chat(
            [
                {
                    "role": "user",
                    "content": "Bonjour.",
                },
                {
                    "role": "system",
                    "content": "Prompt mal placé.",
                },
            ]
        )


# Vérifie que Pydantic refuse
# un rôle absent du contrat ChatMessage.
def test_chat_rejects_unknown_role(
    service: QwenService,
) -> None:
    with pytest.raises(ValidationError):
        service.chat(
            [
                {
                    "role": "developer",
                    "content": "Message invalide.",
                }
            ]
        )


# Vérifie qu'un contenu vide est refusé
# pendant la conversion en ChatMessage.
def test_chat_rejects_empty_message_content(
    service: QwenService,
) -> None:
    with pytest.raises(ValidationError):
        service.chat(
            [
                {
                    "role": "user",
                    "content": "",
                }
            ]
        )