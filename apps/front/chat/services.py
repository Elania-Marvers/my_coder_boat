from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings


# Erreur contrôlée représentant un problème de communication
# ou une réponse invalide provenant de l'API FastAPI.
class ApiClientError(RuntimeError):
    """Erreur pouvant être affichée proprement dans le front."""


# Représente la réponse utile reçue depuis l'API.
# Cette structure appartient au front et ne dépend plus du worker.
@dataclass(frozen=True, slots=True)
class ApiChatResult:
    content: str
    model: str


# Extrait un message lisible depuis une réponse d'erreur FastAPI.
# FastAPI place normalement le détail de l'erreur dans la propriété JSON "detail".
def _extract_error_message(
    response: httpx.Response,
) -> str:
    try:
        payload: Any = response.json()

    except ValueError:
        return (
            "L'API a retourné une erreur "
            f"HTTP {response.status_code} sans message lisible."
        )

    if isinstance(payload, dict):
        detail = payload.get("detail")

        if isinstance(detail, str) and detail.strip():
            return detail.strip()

    return (
        "L'API a retourné une erreur "
        f"HTTP {response.status_code}."
    )


# Envoie l'historique de conversation à FastAPI avec une requête POST,
# vérifie la réponse HTTP puis retourne le texte et le modèle reçus.
def generate_reply(
    history: Sequence[dict[str, str]],
) -> ApiChatResult:
    request_body = {
        "messages": list(history),
    }

    timeout = httpx.Timeout(
        connect=5.0,
        read=settings.MYCODER_API_TIMEOUT_SECONDS,
        write=10.0,
        pool=5.0,
    )

    try:
        # Le client est utilisé comme gestionnaire de contexte
        # afin que ses connexions soient toujours correctement fermées.
        with httpx.Client(
            base_url=settings.MYCODER_API_BASE_URL,
            timeout=timeout,
        ) as client:
            response = client.post(
                "/v1/chat",
                json=request_body,
                headers={
                    "Accept": "application/json",
                },
            )

    # Signale que l'API ou la génération a dépassé
    # la durée maximale autorisée.
    except httpx.TimeoutException as exc:
        raise ApiClientError(
            "L'API a mis trop de temps à répondre. "
            "Le modèle est peut-être encore en cours de chargement."
        ) from exc

    # Signale que Django ne parvient pas à établir
    # ou maintenir la connexion avec FastAPI.
    except httpx.RequestError as exc:
        raise ApiClientError(
            "Impossible de joindre l'API FastAPI. "
            "Vérifie qu'elle est lancée avec `make api` "
            "ou `make dev`."
        ) from exc

    try:
        # Transforme les réponses HTTP 4xx et 5xx
        # en exceptions HTTPX.
        response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        raise ApiClientError(
            _extract_error_message(exc.response)
        ) from exc

    try:
        payload = response.json()

    except ValueError as exc:
        raise ApiClientError(
            "L'API a répondu avec un contenu qui n'est pas du JSON valide."
        ) from exc

    content = payload.get("content")
    model = payload.get("model")

    # Vérifie le format de la réponse avant de l'utiliser
    # dans la session et dans le template Django.
    if (
        not isinstance(content, str)
        or not content.strip()
        or not isinstance(model, str)
        or not model.strip()
    ):
        raise ApiClientError(
            "La réponse de l'API ne contient pas "
            "les champs attendus `content` et `model`."
        )

    return ApiChatResult(
        content=content.strip(),
        model=model.strip(),
    )