from typing import Literal

from pydantic import BaseModel, Field

# Représente un message transmis au modèle.
# Le rôle indique l'auteur et le contenu contient le texte du message.
class ChatMessage(BaseModel):

    # Limite le rôle aux trois valeurs acceptées par les modèles de conversation.
    role: Literal["system", "user", "assistant"]

    # Valide que le contenu n'est pas vide et reste dans une taille raisonnable.
    content: str = Field(min_length=1, max_length=32_000)

# Représente la réponse finale renvoyée par le worker :
# le texte produit et le nom du modèle ayant généré la réponse.
class ChatResult(BaseModel):
    content: str
    model: str
