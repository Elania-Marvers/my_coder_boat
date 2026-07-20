from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# Représente un message reçu par l'API depuis le front.
# Le rôle système est volontairement interdit : le prompt système
# reste contrôlé par l'API et ne peut pas être remplacé par le navigateur.
class ChatMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]

    content: str = Field(
        min_length=1,
        max_length=32_000,
    )


# Représente le corps JSON attendu par la route POST /v1/chat.
# La conversation doit contenir au moins un message et reste limitée
# à vingt messages, comme l'historique actuel du front Django.
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageInput] = Field(
        min_length=1,
        max_length=20,
    )


# Représente la réponse JSON renvoyée au front après la génération.
class ChatResponse(BaseModel):
    content: str
    model: str