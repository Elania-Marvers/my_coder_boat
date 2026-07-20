import logging
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import (
    require_POST,
    require_http_methods,
)

from .forms import ChatForm
from .services import ApiClientError, generate_reply

# Crée un journal associé à ce module afin d'enregistrer
# les erreurs inattendues dans le terminal Django.
logger = logging.getLogger(__name__)

# Noms des emplacements utilisés dans la session Django
# pour conserver la conversation et le nom du modèle.
HISTORY_SESSION_KEY = "mycoder_chat_history"
MODEL_SESSION_KEY = "mycoder_model_name"

# Limite le nombre de messages conservés dans la session
# afin d'éviter que l'historique envoyé au modèle devienne trop volumineux.
MAX_HISTORY_MESSAGES = 20

# Nettoie et valide l'historique récupéré depuis la session Django.
# La fonction ignore les données mal formées, ne garde que les messages
# utilisateur et assistant, supprime les contenus vides et applique
# la limite maximale du nombre de messages.
def _normalise_history(
    raw_history: Any,
) -> list[dict[str, str]]:
    # Une session pouvant contenir une valeur inattendue,
    # on retourne un historique vide si ce n'est pas une liste.
    if not isinstance(raw_history, list):
        return []
    history: list[dict[str, str]] = []

    # Parcourt chaque élément de l'historique pour vérifier sa structure.
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        # Ignore les rôles inconnus afin de ne transmettre au modèle
        # que les messages utilisateur et assistant.
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            history.append(
                {
                    "role": role,
                    "content": content,
                }
            )
    # Ne conserve que les messages les plus récents.
    return history[-MAX_HISTORY_MESSAGES:]

# Affiche la page de conversation et traite l'envoi d'un nouveau message.
# En GET, la fonction affiche l'historique existant.
# En POST, elle valide le formulaire, interroge le worker Qwen,
# enregistre la réponse dans la session puis recharge proprement la page.
@require_http_methods(["GET", "POST"])
def index(request: HttpRequest) -> HttpResponse:

    # Récupère puis sécurise l'historique précédemment stocké dans la session.
    history = _normalise_history(
        request.session.get(
            HISTORY_SESSION_KEY,
            [],
        )
    )

    # Crée un formulaire vide pour une requête GET
    # ou le remplit avec les données envoyées lors d'une requête POST.
    error_message: str | None = None
    form = ChatForm(request.POST or None)

    # Traite le message uniquement lorsqu'il a été envoyé
    # avec une requête POST et que le formulaire est valide.
    if request.method == "POST" and form.is_valid():
        user_message = form.cleaned_data["message"]

        # Ajoute le nouveau message utilisateur à une copie de l'historique.
        conversation = history + [
            {
                "role": "user",
                "content": user_message,
            }
        ]

        # Demande au service du front d'envoyer
        # la conversation à l'API FastAPI.
        # à partir de l'ensemble de la conversation.
        try:
            result = generate_reply(conversation)

        # Affiche les erreurs contrôlées provenant de la communication
        # entre le front Django et l'API FastAPI.
        except ApiClientError as exc:
            error_message = str(exc)

        # Enregistre les erreurs imprévues dans les logs sans exposer
        # leurs détails techniques directement dans l'interface.
        except Exception:
            logger.exception(
                "Erreur inattendue pendant la génération Qwen."
            )

            error_message = (
                "Une erreur inattendue est survenue pendant "
                "la génération. Consulte le terminal Django "
                "pour obtenir le détail."
            )

        # Lorsque la génération réussit, ajoute la réponse du modèle
        # à l'historique puis sauvegarde la conversation dans la session.
        else:
            history = (
                conversation
                + [
                    {
                        "role": "assistant",
                        "content": result.content,
                    }
                ]
            )[-MAX_HISTORY_MESSAGES:]

            request.session[HISTORY_SESSION_KEY] = history
            request.session[MODEL_SESSION_KEY] = result.model

            # Recharge la page avec une requête GET pour éviter
            # qu'un rafraîchissement du navigateur renvoie le formulaire.
            return redirect("chat:index")

    # Affiche le modèle renvoyé par l'API après la première génération.
    # Avant cette première réponse, le front indique simplement
    # que le choix du modèle est géré par FastAPI.
    model_name = request.session.get(
        MODEL_SESSION_KEY,
        "Modèle géré par l’API",
    )

    # Génère la page HTML en lui transmettant le formulaire,
    # l'historique, les éventuelles erreurs et le nom du modèle.
    return render(
        request,
        "chat/index.html",
        {
            "form": form,
            "history": history,
            "error_message": error_message,
            "model_name": model_name,
        },
    )

# Efface l'historique et le nom du modèle stockés dans la session,
# puis redirige l'utilisateur vers une nouvelle conversation vide.
@require_POST
def clear_chat(request: HttpRequest) -> HttpResponse:
    request.session.pop(HISTORY_SESSION_KEY, None)
    request.session.pop(MODEL_SESSION_KEY, None)

    return redirect("chat:index")
