"""
FICHIER :
apps/front/chat/services.py

RÔLE GÉNÉRAL :
Fournit au front Django un client HTTP synchrone
pour communiquer avec l'API FastAPI.

Ce module est la seule couche du front autorisée
à connaître les routes HTTP internes de FastAPI.

IL NE COMMUNIQUE PAS DIRECTEMENT AVEC :
- RabbitMQ ;
- le worker Qwen ;
- Ollama.

APPELÉ PAR :
- apps/front/chat/views.py::submit_job()
- apps/front/chat/views.py::job_status()

APPELLE :
- POST /v1/jobs
  → apps/api/app/main.py::create_job()

- GET /v1/jobs/{job_id}
  → apps/api/app/main.py::get_job()

PIPELINES :
- CHAT_JOB_CREATE
- CHAT_JOB_PUBLISH
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

import httpx
from django.conf import settings


# Représente le contenu JSON générique
# reçu depuis une route FastAPI.
ApiPayload: TypeAlias = dict[str, Any]


# États actuellement reconnus par le front.
#
# Ces valeurs doivent rester cohérentes avec :
# apps/api/app/schemas.py::JobState
ALLOWED_JOB_STATES: Final[frozenset[str]] = frozenset(
    {
        "queued",
        "running",
        "completed",
        "failed",
    }
)


# RÔLE :
# Représente une erreur contrôlée pendant
# la communication entre Django et FastAPI.
#
# LEVÉE PAR :
# - _request_json()
# - _require_non_empty_string()
# - _parse_job_state()
# - _parse_optional_integer()
# - create_job()
# - get_job_status()
#
# INTERCEPTÉE PAR :
# - apps/front/chat/views.py::submit_job()
# - apps/front/chat/views.py::job_status()
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
# - CHAT_JOB_FAILURE
class ApiClientError(RuntimeError):
    """Erreur pouvant être affichée proprement dans le front."""


# RÔLE :
# Contient les informations renvoyées par FastAPI
# immédiatement après la création d'un ticket.
#
# CONSTRUIT PAR :
# - create_job()
#
# UTILISÉ PAR :
# - apps/front/chat/views.py::submit_job()
#
# CORRESPOND À :
# - apps/api/app/schemas.py::JobStatusResponse
#   lorsque l'état initial est queued.
#
# PIPELINE :
# - CHAT_JOB_CREATE
@dataclass(frozen=True, slots=True)
class ApiJobCreated:
    job_id: str
    state: str

    queue_position: int | None
    queue_total: int


# RÔLE :
# Contient l'état actuel d'un ticket FastAPI.
#
# CONSTRUIT PAR :
# - get_job_status()
#
# UTILISÉ PAR :
# - apps/front/chat/views.py::job_status()
# - apps/front/chat/views.py::_finalise_completed_job()
#
# PIPELINES :
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
@dataclass(frozen=True, slots=True)
class ApiJobStatus:
    job_id: str
    state: str

    queue_position: int | None
    queue_total: int
    progress_percent: int | None

    content: str | None
    model: str | None
    error: str | None


# RÔLE :
# Extrait une erreur compréhensible depuis
# une réponse HTTP non réussie de FastAPI.
#
# APPELÉE PAR :
# - _request_json()
#
# LIT :
# - response.status_code
# - response.json()["detail"]
#
# RETOURNE :
# - Le champ FastAPI "detail" lorsqu'il existe ;
# - un message HTTP générique sinon.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
# - CHAT_JOB_FAILURE
def _extract_error_message(
    response: httpx.Response,
) -> str:
    try:
        payload: Any = response.json()

    except ValueError:
        return (
            "L'API a retourné une erreur "
            f"HTTP {response.status_code} "
            "sans contenu JSON lisible."
        )

    if isinstance(payload, dict):
        detail = payload.get("detail")

        if (
            isinstance(detail, str)
            and detail.strip()
        ):
            return detail.strip()

    return (
        "L'API a retourné une erreur "
        f"HTTP {response.status_code}."
    )


# RÔLE :
# Vérifie qu'une propriété JSON contient
# une chaîne non vide.
#
# APPELÉE PAR :
# - create_job()
# - get_job_status()
# - _parse_optional_result()
#
# RETOURNE :
# - La chaîne nettoyée.
#
# ERREUR :
# - ApiClientError lorsque la valeur est absente,
#   vide ou d'un autre type.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _require_non_empty_string(
    payload: ApiPayload,
    field_name: str,
) -> str:
    value = payload.get(field_name)

    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise ApiClientError(
            "La réponse de FastAPI ne contient pas "
            f"un champ `{field_name}` valide."
        )

    return value.strip()


# RÔLE :
# Vérifie que l'état d'un ticket est connu du front.
#
# APPELÉE PAR :
# - create_job()
# - get_job_status()
#
# APPELLE :
# - _require_non_empty_string()
#
# RETOURNE :
# - queued
# - running
# - completed
# - failed
#
# ERREUR :
# - ApiClientError si FastAPI renvoie un état inconnu.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _parse_job_state(
    payload: ApiPayload,
) -> str:
    state = _require_non_empty_string(
        payload,
        "state",
    )

    if state not in ALLOWED_JOB_STATES:
        raise ApiClientError(
            "FastAPI a retourné un état de job "
            f"inconnu : `{state}`."
        )

    return state


# RÔLE :
# Lit un entier facultatif dans une réponse JSON.
#
# APPELÉE PAR :
# - create_job()
# - get_job_status()
#
# RETOURNE :
# - None lorsque le champ est nul ou absent ;
# - un entier positif ou nul sinon.
#
# ERREUR :
# - ApiClientError si la valeur n'est pas un entier
#   ou si elle est négative.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _parse_optional_integer(
    payload: ApiPayload,
    field_name: str,
) -> int | None:
    value = payload.get(field_name)

    if value is None:
        return None

    # bool hérite de int en Python.
    # Il doit donc être refusé explicitement.
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ApiClientError(
            "FastAPI a retourné une valeur invalide "
            f"pour le champ `{field_name}`."
        )

    return value


# RÔLE :
# Lit un entier obligatoire et positif ou nul.
#
# APPELÉE PAR :
# - create_job()
# - get_job_status()
#
# RETOURNE :
# - La valeur entière validée ;
# - la valeur par défaut si le champ est absent.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _parse_integer(
    payload: ApiPayload,
    field_name: str,
    *,
    default: int = 0,
) -> int:
    value = payload.get(
        field_name,
        default,
    )

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ApiClientError(
            "FastAPI a retourné une valeur invalide "
            f"pour le champ `{field_name}`."
        )

    return value


# RÔLE :
# Extrait le résultat final facultatif d'un job.
#
# APPELÉE PAR :
# - get_job_status()
#
# RETOURNE :
# - content et model lorsque le résultat existe ;
# - deux valeurs None lorsque le job n'est pas terminé.
#
# ERREUR :
# - ApiClientError si le champ result existe
#   mais possède une structure invalide.
#
# PIPELINES :
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
def _parse_optional_result(
    payload: ApiPayload,
) -> tuple[str | None, str | None]:
    result = payload.get("result")

    if result is None:
        return None, None

    if not isinstance(result, dict):
        raise ApiClientError(
            "FastAPI a retourné un champ "
            "`result` invalide."
        )

    content = _require_non_empty_string(
        result,
        "content",
    )

    model = _require_non_empty_string(
        result,
        "model",
    )

    return content, model


# RÔLE :
# Extrait le message d'erreur facultatif d'un job.
#
# APPELÉE PAR :
# - get_job_status()
#
# RETOURNE :
# - Le message nettoyé ;
# - None si aucune erreur n'est présente.
#
# PIPELINES :
# - CHAT_JOB_STATUS
# - CHAT_JOB_FAILURE
def _parse_optional_error(
    payload: ApiPayload,
) -> str | None:
    value = payload.get("error")

    if value is None:
        return None

    if not isinstance(value, str):
        raise ApiClientError(
            "FastAPI a retourné un champ "
            "`error` invalide."
        )

    clean_value = value.strip()

    return clean_value or None


# RÔLE :
# Construit le délai HTTP utilisé pour
# les communications courtes avec FastAPI.
#
# APPELÉE PAR :
# - _request_json()
#
# LIT :
# - config.settings.MYCODER_API_TIMEOUT_SECONDS
#
# IMPORTANT :
# Ce délai ne couvre pas toute la génération Qwen.
# Django crée un ticket puis lit son statut
# avec plusieurs requêtes HTTP courtes.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _build_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=5.0,
        read=settings.MYCODER_API_TIMEOUT_SECONDS,
        write=10.0,
        pool=5.0,
    )


# RÔLE :
# Exécute une requête HTTP JSON vers FastAPI,
# gère les erreurs réseau puis valide la réponse.
#
# APPELÉE PAR :
# - create_job()
# - get_job_status()
#
# APPELLE :
# - _build_timeout()
# - httpx.Client.request()
# - _extract_error_message()
#
# CONTACTE :
# - MYCODER_API_BASE_URL
# - Défini dans apps/front/config/settings.py
#
# RETOURNE :
# - Un dictionnaire JSON.
#
# ERREURS CONVERTIES :
# - Timeout HTTP → ApiClientError
# - Erreur réseau → ApiClientError
# - HTTP 4xx/5xx → ApiClientError
# - JSON invalide → ApiClientError
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
# - CHAT_JOB_FAILURE
def _request_json(
    method: str,
    path: str,
    *,
    json_body: ApiPayload | None = None,
) -> ApiPayload:
    try:
        # Le gestionnaire de contexte ferme toujours
        # proprement les connexions HTTPX.
        with httpx.Client(
            base_url=settings.MYCODER_API_BASE_URL,
            timeout=_build_timeout(),
        ) as client:
            response = client.request(
                method,
                path,
                json=json_body,
                headers={
                    "Accept": "application/json",
                },
            )

    except httpx.TimeoutException as exc:
        raise ApiClientError(
            "FastAPI a mis trop de temps à répondre."
        ) from exc

    except httpx.RequestError as exc:
        raise ApiClientError(
            "Impossible de joindre FastAPI. "
            "Vérifie que l'API est lancée "
            "et disponible sur "
            f"{settings.MYCODER_API_BASE_URL}."
        ) from exc

    try:
        response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        raise ApiClientError(
            _extract_error_message(
                exc.response
            )
        ) from exc

    try:
        payload: Any = response.json()

    except ValueError as exc:
        raise ApiClientError(
            "FastAPI n'a pas retourné "
            "une réponse JSON valide."
        ) from exc

    if not isinstance(payload, dict):
        raise ApiClientError(
            "La réponse JSON de FastAPI "
            "ne possède pas la structure attendue."
        )

    return payload


# RÔLE :
# Demande à FastAPI de créer un ticket
# pour une conversation Django.
#
# APPELÉE PAR :
# - apps/front/chat/views.py::submit_job()
#
# APPELLE :
# - _request_json()
# - POST /v1/jobs
# - apps/api/app/main.py::create_job()
# - _require_non_empty_string()
# - _parse_job_state()
# - _parse_optional_integer()
# - _parse_integer()
#
# ENVOIE :
# {
#     "messages": [
#         {
#             "role": "user",
#             "content": "..."
#         }
#     ]
# }
#
# RETOURNE :
# - ApiJobCreated
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_PUBLISH
def create_job(
    history: Sequence[dict[str, str]],
) -> ApiJobCreated:
    payload = _request_json(
        "POST",
        "/v1/jobs",
        json_body={
            "messages": list(history),
        },
    )

    job_id = _require_non_empty_string(
        payload,
        "job_id",
    )

    state = _parse_job_state(
        payload
    )

    queue_position = _parse_optional_integer(
        payload,
        "queue_position",
    )

    queue_total = _parse_integer(
        payload,
        "queue_total",
    )

    return ApiJobCreated(
        job_id=job_id,
        state=state,
        queue_position=queue_position,
        queue_total=queue_total,
    )


# RÔLE :
# Lit l'état actuel d'un ticket FastAPI.
#
# APPELÉE PAR :
# - apps/front/chat/views.py::job_status()
#
# APPELLE :
# - _request_json()
# - GET /v1/jobs/{job_id}
# - apps/api/app/main.py::get_job()
# - _require_non_empty_string()
# - _parse_job_state()
# - _parse_optional_integer()
# - _parse_integer()
# - _parse_optional_result()
# - _parse_optional_error()
#
# RETOURNE :
# - ApiJobStatus
#
# PIPELINES :
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
def get_job_status(
    job_id: str,
) -> ApiJobStatus:
    payload = _request_json(
        "GET",
        f"/v1/jobs/{job_id}",
    )

    response_job_id = _require_non_empty_string(
        payload,
        "job_id",
    )

    state = _parse_job_state(
        payload
    )

    queue_position = _parse_optional_integer(
        payload,
        "queue_position",
    )

    queue_total = _parse_integer(
        payload,
        "queue_total",
    )

    progress_percent = _parse_optional_integer(
        payload,
        "progress_percent",
    )

    content, model = _parse_optional_result(
        payload
    )

    error = _parse_optional_error(
        payload
    )

    return ApiJobStatus(
        job_id=response_job_id,
        state=state,
        queue_position=queue_position,
        queue_total=queue_total,
        progress_percent=progress_percent,
        content=content,
        model=model,
        error=error,
    )