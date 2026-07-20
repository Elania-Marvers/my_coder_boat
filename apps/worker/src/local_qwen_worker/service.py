from collections.abc import Mapping, Sequence

from .ollama_client import OllamaClient
from .schemas import ChatMessage, ChatResult

# Type accepté pour un message : objet ChatMessage déjà validé
# ou dictionnaire qui devra être validé par Pydantic.
MessageInput = ChatMessage | Mapping[str, str]

# Fournit l'interface principale utilisée par le front
# pour converser avec le modèle sans connaître les détails d'Ollama.
class QwenService:
    
    # Initialise le service avec un client Ollama fourni
    # ou en crée automatiquement un nouveau.
    def __init__(self, client: OllamaClient | None = None) -> None:
        self.client = client or OllamaClient()

    # Valide tous les messages, ajoute le prompt système lorsqu'il manque,
    # vérifie que la conversation n'est pas vide puis transmet la demande
    # au client Ollama.
    def chat(
        self,
        messages: Sequence[MessageInput],
        system_prompt: str | None = None,
    ) -> ChatResult:
        
        # Transforme chaque message reçu en un objet ChatMessage validé.
        validated_messages = [
            self._validate_message(message)
            for message in messages
        ]

        # Vérifie si la conversation contient déjà un message système.
        has_system_prompt = any(
            message.role == "system"
            for message in validated_messages
        )

        # Ajoute le prompt système au début de la conversation
        # uniquement lorsqu'aucun autre message système n'est déjà présent.
        if system_prompt and not has_system_prompt:
            validated_messages.insert(
                0,
                ChatMessage(
                    role="system",
                    content=system_prompt,
                ),
            )

        # Empêche l'envoi d'une requête sans aucun message au modèle.
        if not validated_messages:
            raise ValueError(
                "La conversation ne peut pas être vide."
            )

        # Délègue la génération effective au client Ollama.
        return self.client.chat(validated_messages)

    # Retourne directement un ChatMessage déjà validé
    # ou transforme un dictionnaire en ChatMessage avec Pydantic.
    @staticmethod
    def _validate_message(message: MessageInput) -> ChatMessage:
        if isinstance(message, ChatMessage):
            return message

        return ChatMessage.model_validate(message)
