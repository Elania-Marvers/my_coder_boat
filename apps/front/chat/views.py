"""
FICHIER :
apps/front/chat/views.py

RÔLE GÉNÉRAL :
Coordonne les requêtes HTTP reçues par le front Django.

Ce module ne communique jamais directement avec RabbitMQ,
le worker Qwen ou Ollama.

Il délègue les communications avec FastAPI au fichier :

apps/front/chat/services.py

ROUTES ASSOCIÉES :
- GET  /
- POST /jobs/submit/
- GET  /jobs/<job_id>/
- POST /clear/

PIPELINES :
- CHAT_PAGE_DISPLAY
- CHAT_JOB_CREATE
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
- CHAT_CLEAR
"""

from typing import Any, TypeAlias
from uuid import UUID

from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import (
    redirect,
    render,
)
from django.urls import reverse
from django.views.decorators.http import (
    require_GET,
    require_POST,
)

from .forms import ChatForm
from .services import (
    ApiClientError,
    ApiJobStatus,
    create_job,
    get_job_status,
)


# Représente un message conservé dans la session Django.
#
# Exemple :
# {
#     "role": "user",
#     "content": "Quelle est la différence entre..."
# }
ChatMessage: TypeAlias = dict[str, str]


# Représente la liste des messages composant une conversation.
ChatHistory: TypeAlias = list[ChatMessage]


# Clé utilisée dans la session Django
# pour conserver les messages de la conversation.
HISTORY_SESSION_KEY = "mycoder_chat_history"


# Clé utilisée dans la session Django
# pour conserver le nom du dernier modèle ayant répondu.
MODEL_SESSION_KEY = "mycoder_model_name"


# Clé utilisée dans la session Django
# pour conserver l'identifiant du job encore actif.
ACTIVE_JOB_SESSION_KEY = "mycoder_active_job"


# Limite le nombre de messages conservés dans la session
# et transmis au modèle lors de la demande suivante.
MAX_HISTORY_MESSAGES = 20


# RÔLE :
# Nettoie une valeur provenant de la session Django
# et la transforme en historique exploitable.
#
# APPELÉE PAR :
# - _get_history()
# - _save_history()
# - _build_conversation()
#
# APPELLE :
# - Aucune fonction externe.
#
# FILTRE :
# - les éléments qui ne sont pas des dictionnaires ;
# - les rôles autres que user et assistant ;
# - les contenus qui ne sont pas des chaînes ;
# - les messages vides ;
# - les messages dépassant la limite d'historique.
#
# RETOURNE :
# - Une liste de messages sécurisée.
#
# PIPELINES :
# - CHAT_PAGE_DISPLAY
# - CHAT_JOB_CREATE
# - CHAT_JOB_COMPLETE
def _normalise_history(
    raw_history: Any,
) -> ChatHistory:
    if not isinstance(raw_history, list):
        return []

    history: ChatHistory = []

    for item in raw_history:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = item.get("content")

        if role not in {
            "user",
            "assistant",
        }:
            continue

        if not isinstance(content, str):
            continue

        clean_content = content.strip()

        if not clean_content:
            continue

        history.append(
            {
                "role": role,
                "content": clean_content,
            }
        )

    return history[-MAX_HISTORY_MESSAGES:]


# RÔLE :
# Lit et sécurise l'historique actuellement enregistré
# dans la session du navigateur.
#
# APPELÉE PAR :
# - index()
# - submit_job()
# - _finalise_completed_job()
#
# APPELLE :
# - _normalise_history()
#
# LIT :
# - Session Django : mycoder_chat_history
#
# RETOURNE :
# - L'historique de conversation sécurisé.
#
# PIPELINES :
# - CHAT_PAGE_DISPLAY
# - CHAT_JOB_CREATE
# - CHAT_JOB_COMPLETE
def _get_history(
    request: HttpRequest,
) -> ChatHistory:
    raw_history = request.session.get(
        HISTORY_SESSION_KEY,
        [],
    )

    return _normalise_history(
        raw_history
    )


# RÔLE :
# Enregistre un historique propre et limité
# dans la session Django.
#
# APPELÉE PAR :
# - submit_job()
# - _finalise_completed_job()
#
# APPELLE :
# - _normalise_history()
#
# MODIFIE :
# - Session Django : mycoder_chat_history
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_COMPLETE
def _save_history(
    request: HttpRequest,
    history: ChatHistory,
) -> None:
    request.session[HISTORY_SESSION_KEY] = (
        _normalise_history(history)
    )


# RÔLE :
# Récupère l'identifiant du job actuellement associé
# à la session Django et vérifie qu'il s'agit d'un UUID valide.
#
# APPELÉE PAR :
# - index()
# - submit_job()
# - job_status()
#
# LIT :
# - Session Django : mycoder_active_job
#
# RETOURNE :
# - L'identifiant sous forme de chaîne ;
# - None si aucun job valide n'est actif.
#
# PIPELINES :
# - CHAT_PAGE_DISPLAY
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _get_active_job_id(
    request: HttpRequest,
) -> str | None:
    raw_job_id = request.session.get(
        ACTIVE_JOB_SESSION_KEY
    )

    if not isinstance(raw_job_id, str):
        return None

    try:
        UUID(raw_job_id)

    except ValueError:
        return None

    return raw_job_id


# RÔLE :
# Associe un nouveau job FastAPI à la session Django.
#
# APPELÉE PAR :
# - submit_job()
#
# MODIFIE :
# - Session Django : mycoder_active_job
#
# PIPELINE :
# - CHAT_JOB_CREATE
def _set_active_job_id(
    request: HttpRequest,
    job_id: str,
) -> None:
    request.session[ACTIVE_JOB_SESSION_KEY] = (
        job_id
    )


# RÔLE :
# Retire l'identifiant du job actif de la session Django.
#
# APPELÉE PAR :
# - _finalise_completed_job()
# - job_status()
# - clear_chat()
#
# MODIFIE :
# - Session Django : mycoder_active_job
#
# PIPELINES :
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
# - CHAT_CLEAR
def _clear_active_job(
    request: HttpRequest,
) -> None:
    request.session.pop(
        ACTIVE_JOB_SESSION_KEY,
        None,
    )


# RÔLE :
# Construit l'URL Django utilisée par le navigateur
# pour suivre l'état d'un job.
#
# APPELÉE PAR :
# - index()
# - submit_job()
#
# APPELLE :
# - django.urls.reverse()
# - Route chat:job-status
#
# RETOURNE :
# - /jobs/<job_id>/ lorsque l'identifiant est valide ;
# - une chaîne vide lorsqu'aucun job ne peut être suivi.
#
# PIPELINE :
# - CHAT_JOB_STATUS
def _build_status_url(
    job_id: str | None,
) -> str:
    if not job_id:
        return ""

    try:
        parsed_job_id = UUID(job_id)

    except ValueError:
        return ""

    return reverse(
        "chat:job-status",
        kwargs={
            "job_id": parsed_job_id,
        },
    )


# RÔLE :
# Ajoute le nouveau message utilisateur
# à l'historique existant.
#
# APPELÉE PAR :
# - submit_job()
#
# APPELLE :
# - _normalise_history()
#
# RETOURNE :
# - La conversation à envoyer à FastAPI.
#
# PIPELINE :
# - CHAT_JOB_CREATE
def _build_conversation(
    history: ChatHistory,
    user_message: str,
) -> ChatHistory:
    conversation = (
        history
        + [
            {
                "role": "user",
                "content": user_message,
            }
        ]
    )

    return _normalise_history(
        conversation
    )


# RÔLE :
# Ajoute la réponse finale du worker dans l'historique,
# mémorise le modèle utilisé puis clôture le job actif.
#
# APPELÉE PAR :
# - job_status()
#
# APPELLE :
# - _get_history()
# - _save_history()
# - _clear_active_job()
#
# MODIFIE :
# - Session Django : mycoder_chat_history
# - Session Django : mycoder_model_name
# - Session Django : mycoder_active_job
#
# PRÉCONDITION :
# - job.content ne doit pas être vide ;
# - job.model ne doit pas être vide.
#
# PIPELINE :
# - CHAT_JOB_COMPLETE
def _finalise_completed_job(
    request: HttpRequest,
    job: ApiJobStatus,
) -> None:
    if not job.content or not job.model:
        raise ValueError(
            "Le job terminé ne contient pas "
            "de résultat exploitable."
        )

    history = _get_history(request)

    history.append(
        {
            "role": "assistant",
            "content": job.content,
        }
    )

    _save_history(
        request,
        history,
    )

    request.session[MODEL_SESSION_KEY] = (
        job.model
    )

    _clear_active_job(request)


# RÔLE :
# Affiche la page principale de conversation.
#
# APPELÉE PAR :
# - apps/front/chat/urls.py
# - Route GET /
#
# APPELLE :
# - _get_history()
# - _get_active_job_id()
# - _build_status_url()
# - django.shortcuts.render()
# - apps/front/chat/forms.py::ChatForm()
#
# LIT :
# - Historique de la session Django ;
# - modèle actif ;
# - identifiant du job actif.
#
# RETOURNE :
# - Le template apps/front/chat/templates/chat/index.html
#
# PIPELINES :
# - CHAT_PAGE_DISPLAY
# - CHAT_JOB_STATUS
@require_GET
def index(
    request: HttpRequest,
) -> HttpResponse:
    history = _get_history(request)

    active_job_id = _get_active_job_id(
        request
    )

    model_name = request.session.get(
        MODEL_SESSION_KEY,
        "Modèle géré par l’API",
    )

    return render(
        request,
        "chat/index.html",
        {
            "form": ChatForm(),
            "history": history,
            "model_name": model_name,
            "active_job_id": active_job_id,
            "active_job_status_url": (
                _build_status_url(
                    active_job_id
                )
            ),
        },
    )


# RÔLE :
# Valide le message envoyé par le navigateur,
# construit la conversation puis demande à FastAPI
# de créer un ticket RabbitMQ.
#
# APPELÉE PAR :
# - apps/front/chat/urls.py
# - Route POST /jobs/submit/
# - apps/front/chat/static/chat/chat.js::submitJob()
#
# APPELLE :
# - apps/front/chat/forms.py::ChatForm
# - _get_active_job_id()
# - _get_history()
# - _build_conversation()
# - apps/front/chat/services.py::create_job()
# - _save_history()
# - _set_active_job_id()
# - _build_status_url()
#
# MODIFIE :
# - Session Django : mycoder_chat_history
# - Session Django : mycoder_active_job
#
# RETOURNE :
# - HTTP 202 avec le ticket créé ;
# - HTTP 400 si le formulaire est invalide ;
# - HTTP 409 si un job est déjà actif ;
# - HTTP 502 si FastAPI est indisponible.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_PUBLISH
@require_POST
def submit_job(
    request: HttpRequest,
) -> JsonResponse:
    active_job_id = _get_active_job_id(
        request
    )

    # Une conversation ne peut suivre qu'un seul job
    # à la fois dans cette première version.
    if active_job_id:
        return JsonResponse(
            {
                "error": (
                    "Un job est déjà en cours "
                    "dans cette conversation."
                ),
            },
            status=409,
        )

    form = ChatForm(
        request.POST
    )

    if not form.is_valid():
        message_errors = form.errors.get(
            "message",
            ["Message invalide."],
        )

        return JsonResponse(
            {
                "error": str(
                    message_errors[0]
                ),
            },
            status=400,
        )

    history = _get_history(request)

    conversation = _build_conversation(
        history,
        form.cleaned_data["message"],
    )

    try:
        created_job = create_job(
            conversation
        )

    except ApiClientError as exc:
        return JsonResponse(
            {
                "error": str(exc),
            },
            status=502,
        )

    # Le message utilisateur est enregistré immédiatement
    # afin qu'il reste visible pendant la génération.
    _save_history(
        request,
        conversation,
    )

    _set_active_job_id(
        request,
        created_job.job_id,
    )

    return JsonResponse(
        {
            "job_id": created_job.job_id,
            "state": created_job.state,
            "queue_position": (
                created_job.queue_position
            ),
            "queue_total": (
                created_job.queue_total
            ),
            "progress_percent": 0,
            "status_url": _build_status_url(
                created_job.job_id
            ),
        },
        status=202,
    )


# RÔLE :
# Interroge FastAPI pour obtenir l'état actuel du ticket.
#
# Lorsque le ticket est terminé, la fonction ajoute
# la réponse Qwen dans la session Django.
#
# APPELÉE PAR :
# - apps/front/chat/urls.py
# - Route GET /jobs/<job_id>/
# - apps/front/chat/static/chat/chat.js::pollJob()
#
# APPELLE :
# - _get_active_job_id()
# - apps/front/chat/services.py::get_job_status()
# - _finalise_completed_job()
# - _clear_active_job()
#
# MODIFIE :
# - L'historique lorsque le job est terminé ;
# - le modèle actif ;
# - l'identifiant du job actif.
#
# RETOURNE :
# - HTTP 200 avec l'état actuel ;
# - HTTP 404 si le job n'appartient pas à la session ;
# - HTTP 502 si FastAPI est indisponible
#   ou si le résultat final est invalide.
#
# PIPELINES :
# - CHAT_JOB_STATUS
# - CHAT_JOB_COMPLETE
# - CHAT_JOB_FAILURE
@require_GET
def job_status(
    request: HttpRequest,
    job_id: UUID,
) -> JsonResponse:
    active_job_id = _get_active_job_id(
        request
    )

    # Empêche une session de consulter un job
    # qui ne lui appartient pas.
    if active_job_id != str(job_id):
        return JsonResponse(
            {
                "error": (
                    "Ce job n'est pas associé "
                    "à cette conversation."
                ),
            },
            status=404,
        )

    try:
        job = get_job_status(
            str(job_id)
        )

    except ApiClientError as exc:
        return JsonResponse(
            {
                "error": str(exc),
            },
            status=502,
        )

    should_reload = False

    if job.state == "completed":
        try:
            _finalise_completed_job(
                request,
                job,
            )

        except ValueError as exc:
            _clear_active_job(request)

            return JsonResponse(
                {
                    "error": str(exc),
                },
                status=502,
            )

        should_reload = True

    elif job.state == "failed":
        # Le job n'est plus actif après un échec définitif.
        _clear_active_job(request)

    return JsonResponse(
        {
            "job_id": job.job_id,
            "state": job.state,
            "queue_position": (
                job.queue_position
            ),
            "queue_total": (
                job.queue_total
            ),
            "progress_percent": (
                job.progress_percent
            ),
            "error": job.error,
            "reload": should_reload,
        }
    )


# RÔLE :
# Efface les informations de conversation
# conservées dans la session Django.
#
# APPELÉE PAR :
# - apps/front/chat/urls.py
# - Route POST /clear/
# - Formulaire « Nouvelle conversation »
#
# APPELLE :
# - _clear_active_job()
# - django.shortcuts.redirect()
#
# MODIFIE :
# - Session Django : mycoder_chat_history
# - Session Django : mycoder_model_name
# - Session Django : mycoder_active_job
#
# IMPORTANT :
# Effacer la session n'annule pas un message
# déjà publié dans RabbitMQ.
#
# PIPELINE :
# - CHAT_CLEAR
@require_POST
def clear_chat(
    request: HttpRequest,
) -> HttpResponse:
    request.session.pop(
        HISTORY_SESSION_KEY,
        None,
    )

    request.session.pop(
        MODEL_SESSION_KEY,
        None,
    )

    _clear_active_job(request)

    return redirect(
        "chat:index"
    )