"""
FICHIER :
tests/unit/front/test_views.py

RÔLE GÉNÉRAL :
Teste les vues HTTP de l'application Django chat.

Ces tests vérifient notamment :

- l'affichage de la page principale ;
- le nettoyage de l'historique ;
- la création d'un ticket FastAPI ;
- l'enregistrement du ticket dans la session ;
- le refus de plusieurs jobs simultanés ;
- le suivi des états queued et running ;
- la finalisation d'un job completed ;
- la suppression d'un job failed ;
- la gestion des erreurs FastAPI ;
- la remise à zéro d'une conversation ;
- les méthodes HTTP autorisées.

AUCUN SERVICE RÉEL N'EST DÉMARRÉ :

- FastAPI n'est pas lancé ;
- RabbitMQ n'est pas lancé ;
- le worker n'est pas lancé ;
- Ollama n'est pas lancé.

Les fonctions importées depuis chat.services
sont remplacées par des objets Mock.

CIRCULATION TESTÉE :

navigateur simulé
→ vue Django
→ faux service FastAPI
→ réponse Django
→ session du navigateur

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_PAGE_DISPLAY
- CHAT_JOB_CREATE
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
- CHAT_CLEAR
"""

from unittest.mock import Mock
from uuid import UUID

import pytest
from django.test import Client
from django.urls import reverse

from chat import views
from chat.services import (
    ApiClientError,
    ApiJobCreated,
    ApiJobStatus,
)
from chat.views import (
    ACTIVE_JOB_SESSION_KEY,
    HISTORY_SESSION_KEY,
    MAX_HISTORY_MESSAGES,
    MODEL_SESSION_KEY,
    _build_status_url,
    _normalise_history,
)


# Tous les tests peuvent utiliser la base temporaire Django.
#
# Elle est notamment nécessaire pour :
#
# - les sessions ;
# - django.test.Client ;
# - les middlewares Django.
pytestmark = pytest.mark.django_db


# Identifiant principal utilisé par les tests.
JOB_ID = UUID(
    "12345678-1234-5678-1234-567812345678"
)


# Second identifiant permettant de simuler
# un job appartenant à une autre conversation.
OTHER_JOB_ID = UUID(
    "87654321-4321-8765-4321-876543218765"
)


# RÔLE :
# Fournit un navigateur Django simulé
# indépendant pour chaque test.
#
# RETOURNE :
# - django.test.Client
@pytest.fixture
def client() -> Client:
    return Client()


# RÔLE :
# Enregistre plusieurs valeurs dans
# la session du navigateur simulé.
#
# APPELÉE PAR :
# - les tests de reprise d'un job ;
# - les tests de finalisation ;
# - les tests de nettoyage.
#
# MODIFIE :
# - client.session
def _save_session_values(
    client: Client,
    values: dict[str, object],
) -> None:
    session = client.session

    for key, value in values.items():
        session[key] = value

    session.save()


# RÔLE :
# Construit l'URL Django de suivi
# pour l'identifiant principal.
#
# RETOURNE :
# - /jobs/<uuid>/
def _job_status_url() -> str:
    return reverse(
        "chat:job-status",
        kwargs={
            "job_id": JOB_ID,
        },
    )


# ---------------------------------------------------------------------------
# Nettoyage et limitation de l'historique
# ---------------------------------------------------------------------------


# Vérifie que les entrées incorrectes
# d'une session sont ignorées.
def test_normalise_history_filters_invalid_items() -> None:
    history = _normalise_history(
        [
            {
                "role": "user",
                "content": "  Bonjour.  ",
            },
            {
                "role": "assistant",
                "content": "  Bonjour humain.  ",
            },
            {
                "role": "system",
                "content": "Prompt interdit.",
            },
            {
                "role": "user",
                "content": "   ",
            },
            {
                "role": "user",
                "content": 42,
            },
            "entrée invalide",
            None,
        ]
    )

    assert history == [
        {
            "role": "user",
            "content": "Bonjour.",
        },
        {
            "role": "assistant",
            "content": "Bonjour humain.",
        },
    ]


# Vérifie que seuls les vingt messages
# les plus récents sont conservés.
def test_normalise_history_keeps_last_twenty_messages() -> None:
    raw_history = [
        {
            "role": "user",
            "content": f"Message {index}",
        }
        for index in range(
            MAX_HISTORY_MESSAGES + 5
        )
    ]

    history = _normalise_history(
        raw_history
    )

    assert len(history) == MAX_HISTORY_MESSAGES

    assert history[0]["content"] == "Message 5"

    assert history[-1]["content"] == (
        f"Message {MAX_HISTORY_MESSAGES + 4}"
    )


# Vérifie qu'un identifiant absent ou invalide
# ne produit aucune URL de suivi.
def test_build_status_url_rejects_invalid_identifier() -> None:
    assert _build_status_url(None) == ""
    assert _build_status_url("") == ""
    assert _build_status_url("identifiant-invalide") == ""


# ---------------------------------------------------------------------------
# Affichage de la page principale
# ---------------------------------------------------------------------------


# Vérifie que la page principale
# est accessible avec une conversation vide.
def test_index_displays_empty_conversation(
    client: Client,
) -> None:
    response = client.get(
        reverse("chat:index")
    )

    assert response.status_code == 200

    assert response.context["history"] == []

    assert (
        response.context["active_job_status_url"]
        == ""
    )

    assert response.context["model_name"] == (
        "Modèle géré par l’API"
    )


# Vérifie qu'un job stocké dans la session
# est exposé au JavaScript pour reprendre le polling.
def test_index_exposes_active_job_status_url(
    client: Client,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    response = client.get(
        reverse("chat:index")
    )

    assert response.status_code == 200

    assert (
        response.context["active_job_id"]
        == str(JOB_ID)
    )

    assert (
        response.context["active_job_status_url"]
        == _job_status_url()
    )


# ---------------------------------------------------------------------------
# Création d'un ticket
# ---------------------------------------------------------------------------


# Vérifie le chemin normal :
#
# formulaire valide
# → création FastAPI
# → historique enregistré
# → job actif enregistré
# → HTTP 202.
def test_submit_job_creates_ticket_and_updates_session(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_job_mock = Mock(
        return_value=ApiJobCreated(
            job_id=str(JOB_ID),
            state="queued",
            queue_position=2,
            queue_total=4,
        )
    )

    monkeypatch.setattr(
        views,
        "create_job",
        create_job_mock,
    )

    response = client.post(
        reverse("chat:submit-job"),
        data={
            "message": (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        },
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 202

    payload = response.json()

    assert payload == {
        "job_id": str(JOB_ID),
        "state": "queued",
        "queue_position": 2,
        "queue_total": 4,
        "progress_percent": 0,
        "status_url": _job_status_url(),
    }

    create_job_mock.assert_called_once_with(
        [
            {
                "role": "user",
                "content": (
                    "Explique le fonctionnement "
                    "d'une frégate."
                ),
            }
        ]
    )

    session = client.session

    assert session[HISTORY_SESSION_KEY] == [
        {
            "role": "user",
            "content": (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        }
    ]

    assert session[ACTIVE_JOB_SESSION_KEY] == (
        str(JOB_ID)
    )


# Vérifie qu'un formulaire vide
# est rejeté sans contacter FastAPI.
def test_submit_job_rejects_empty_message(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_job_mock = Mock()

    monkeypatch.setattr(
        views,
        "create_job",
        create_job_mock,
    )

    response = client.post(
        reverse("chat:submit-job"),
        data={
            "message": "   ",
        },
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 400

    assert "error" in response.json()

    create_job_mock.assert_not_called()

    assert HISTORY_SESSION_KEY not in client.session
    assert ACTIVE_JOB_SESSION_KEY not in client.session


# Vérifie qu'une conversation ne peut pas
# créer un second ticket pendant le traitement du premier.
def test_submit_job_rejects_when_job_is_already_active(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    create_job_mock = Mock()

    monkeypatch.setattr(
        views,
        "create_job",
        create_job_mock,
    )

    response = client.post(
        reverse("chat:submit-job"),
        data={
            "message": "Nouvelle question.",
        },
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 409

    assert response.json()["error"] == (
        "Un job est déjà en cours "
        "dans cette conversation."
    )

    create_job_mock.assert_not_called()


# Vérifie qu'une erreur du client FastAPI
# devient une réponse HTTP 502.
#
# L'historique ne doit pas être modifié
# car le ticket n'a jamais été créé.
def test_submit_job_translates_api_error_without_mutating_session(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_history = [
        {
            "role": "user",
            "content": "Ancienne question.",
        },
        {
            "role": "assistant",
            "content": "Ancienne réponse.",
        },
    ]

    _save_session_values(
        client,
        {
            HISTORY_SESSION_KEY: initial_history,
        },
    )

    create_job_mock = Mock(
        side_effect=ApiClientError(
            "FastAPI est indisponible."
        )
    )

    monkeypatch.setattr(
        views,
        "create_job",
        create_job_mock,
    )

    response = client.post(
        reverse("chat:submit-job"),
        data={
            "message": "Nouvelle question.",
        },
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 502

    assert response.json()["error"] == (
        "FastAPI est indisponible."
    )

    assert (
        client.session[HISTORY_SESSION_KEY]
        == initial_history
    )

    assert ACTIVE_JOB_SESSION_KEY not in client.session


# Vérifie que la route de création
# refuse les requêtes GET.
def test_submit_job_requires_post(
    client: Client,
) -> None:
    response = client.get(
        reverse("chat:submit-job")
    )

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Propriété et suivi d'un ticket
# ---------------------------------------------------------------------------


# Vérifie qu'une session ne peut pas consulter
# un job qui ne lui appartient pas.
def test_job_status_rejects_job_not_owned_by_session(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(
                OTHER_JOB_ID
            ),
        },
    )

    get_status_mock = Mock()

    monkeypatch.setattr(
        views,
        "get_job_status",
        get_status_mock,
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 404

    assert response.json()["error"] == (
        "Ce job n'est pas associé "
        "à cette conversation."
    )

    get_status_mock.assert_not_called()


# Vérifie la transmission d'un état queued
# sans modification de la session.
def test_job_status_returns_queued_state(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    get_status_mock = Mock(
        return_value=ApiJobStatus(
            job_id=str(JOB_ID),
            state="queued",
            queue_position=2,
            queue_total=4,
            progress_percent=0,
            content=None,
            model=None,
            error=None,
        )
    )

    monkeypatch.setattr(
        views,
        "get_job_status",
        get_status_mock,
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 200

    assert response.json() == {
        "job_id": str(JOB_ID),
        "state": "queued",
        "queue_position": 2,
        "queue_total": 4,
        "progress_percent": 0,
        "error": None,
        "reload": False,
    }

    assert client.session[
        ACTIVE_JOB_SESSION_KEY
    ] == str(JOB_ID)

    get_status_mock.assert_called_once_with(
        str(JOB_ID)
    )


# Vérifie la transmission d'un état running
# avec une progression indéterminée.
def test_job_status_returns_running_state(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    monkeypatch.setattr(
        views,
        "get_job_status",
        Mock(
            return_value=ApiJobStatus(
                job_id=str(JOB_ID),
                state="running",
                queue_position=None,
                queue_total=0,
                progress_percent=None,
                content=None,
                model=None,
                error=None,
            )
        ),
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["state"] == "running"
    assert payload["queue_position"] is None
    assert payload["progress_percent"] is None
    assert payload["reload"] is False

    assert client.session[
        ACTIVE_JOB_SESSION_KEY
    ] == str(JOB_ID)


# ---------------------------------------------------------------------------
# Finalisation d'un ticket completed
# ---------------------------------------------------------------------------


# Vérifie qu'une réponse completed :
#
# - ajoute un message assistant ;
# - mémorise le modèle ;
# - supprime le job actif ;
# - demande au navigateur de recharger la page.
def test_job_status_completed_updates_history_and_clears_active_job(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_history = [
        {
            "role": "user",
            "content": (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        }
    ]

    _save_session_values(
        client,
        {
            HISTORY_SESSION_KEY: initial_history,
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    monkeypatch.setattr(
        views,
        "get_job_status",
        Mock(
            return_value=ApiJobStatus(
                job_id=str(JOB_ID),
                state="completed",
                queue_position=None,
                queue_total=0,
                progress_percent=100,
                content=(
                    "Une frégate est un navire "
                    "militaire polyvalent."
                ),
                model="qwen-test-model",
                error=None,
            )
        ),
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["state"] == "completed"
    assert payload["progress_percent"] == 100
    assert payload["reload"] is True

    session = client.session

    assert session[HISTORY_SESSION_KEY] == [
        {
            "role": "user",
            "content": (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Une frégate est un navire "
                "militaire polyvalent."
            ),
        },
    ]

    assert session[MODEL_SESSION_KEY] == (
        "qwen-test-model"
    )

    assert ACTIVE_JOB_SESSION_KEY not in session


# Vérifie qu'un état completed incomplet
# est refusé puis retire le job actif.
def test_job_status_completed_without_result_returns_502(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_history = [
        {
            "role": "user",
            "content": "Question en attente.",
        }
    ]

    _save_session_values(
        client,
        {
            HISTORY_SESSION_KEY: initial_history,
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    monkeypatch.setattr(
        views,
        "get_job_status",
        Mock(
            return_value=ApiJobStatus(
                job_id=str(JOB_ID),
                state="completed",
                queue_position=None,
                queue_total=0,
                progress_percent=100,
                content=None,
                model=None,
                error=None,
            )
        ),
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 502

    assert response.json()["error"] == (
        "Le job terminé ne contient pas "
        "de résultat exploitable."
    )

    session = client.session

    assert (
        session[HISTORY_SESSION_KEY]
        == initial_history
    )

    assert ACTIVE_JOB_SESSION_KEY not in session
    assert MODEL_SESSION_KEY not in session


# ---------------------------------------------------------------------------
# Finalisation d'un ticket failed
# ---------------------------------------------------------------------------


# Vérifie qu'un échec définitif
# supprime le job actif de la session.
def test_job_status_failed_clears_active_job(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    monkeypatch.setattr(
        views,
        "get_job_status",
        Mock(
            return_value=ApiJobStatus(
                job_id=str(JOB_ID),
                state="failed",
                queue_position=None,
                queue_total=0,
                progress_percent=None,
                content=None,
                model=None,
                error="Ollama est indisponible.",
            )
        ),
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["state"] == "failed"

    assert payload["error"] == (
        "Ollama est indisponible."
    )

    assert payload["reload"] is False

    assert (
        ACTIVE_JOB_SESSION_KEY
        not in client.session
    )


# Vérifie qu'une erreur temporaire
# pendant la lecture FastAPI retourne HTTP 502.
#
# Le job actif reste conservé afin que le navigateur
# puisse réessayer ultérieurement.
def test_job_status_api_error_preserves_active_job(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_session_values(
        client,
        {
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    monkeypatch.setattr(
        views,
        "get_job_status",
        Mock(
            side_effect=ApiClientError(
                "FastAPI ne répond pas."
            )
        ),
    )

    response = client.get(
        _job_status_url(),
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 502

    assert response.json()["error"] == (
        "FastAPI ne répond pas."
    )

    assert client.session[
        ACTIVE_JOB_SESSION_KEY
    ] == str(JOB_ID)


# Vérifie que la route de suivi
# refuse les requêtes POST.
def test_job_status_requires_get(
    client: Client,
) -> None:
    response = client.post(
        _job_status_url()
    )

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Nettoyage de la conversation
# ---------------------------------------------------------------------------


# Vérifie que « Nouvelle conversation »
# retire toutes les informations du chat.
def test_clear_chat_removes_session_values(
    client: Client,
) -> None:
    _save_session_values(
        client,
        {
            HISTORY_SESSION_KEY: [
                {
                    "role": "user",
                    "content": "Question.",
                },
                {
                    "role": "assistant",
                    "content": "Réponse.",
                },
            ],
            MODEL_SESSION_KEY: "qwen-test-model",
            ACTIVE_JOB_SESSION_KEY: str(JOB_ID),
        },
    )

    response = client.post(
        reverse("chat:clear")
    )

    assert response.status_code == 302
    assert response.url == reverse("chat:index")

    session = client.session

    assert HISTORY_SESSION_KEY not in session
    assert MODEL_SESSION_KEY not in session
    assert ACTIVE_JOB_SESSION_KEY not in session


# Vérifie que le nettoyage exige une requête POST.
#
# Cela empêche un simple lien ou un robot
# de supprimer accidentellement une conversation.
def test_clear_chat_requires_post(
    client: Client,
) -> None:
    response = client.get(
        reverse("chat:clear")
    )

    assert response.status_code == 405