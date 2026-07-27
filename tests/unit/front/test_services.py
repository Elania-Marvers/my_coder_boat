"""
FICHIER :
tests/unit/front/test_services.py

RÔLE GÉNÉRAL :
Teste le client HTTP utilisé par Django
pour communiquer avec l'API FastAPI.

Aucune véritable requête réseau n'est exécutée.

Le client httpx.Client est remplacé par un faux client
capable de :

- enregistrer les requêtes reçues ;
- retourner une réponse HTTP simulée ;
- lever une erreur réseau simulée ;
- vérifier la fermeture du client.

CIRCULATION TESTÉE :

apps/front/chat/views.py
→ apps/front/chat/services.py
→ faux client HTTP
→ réponse FastAPI simulée
→ ApiJobCreated ou ApiJobStatus

ÉLÉMENTS TESTÉS :

- construction du timeout HTTP ;
- création d'un ticket ;
- lecture des quatre états de ticket ;
- extraction du résultat Qwen ;
- extraction des erreurs ;
- validation des entiers ;
- validation des états ;
- erreurs HTTP ;
- timeouts ;
- erreurs réseau ;
- JSON invalide ;
- structure JSON invalide ;
- fermeture du client HTTP.

APPELÉ PAR :
- Makefile::test
- uv run pytest

PIPELINES :
- TEST
- CHAT_JOB_CREATE
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

from typing import Any

import httpx
import pytest

from chat import services
from chat.services import (
    ApiClientError,
    ApiJobCreated,
    ApiJobStatus,
    _build_timeout,
    _request_json,
    create_job,
    get_job_status,
)


# Identifiant stable utilisé dans les réponses
# FastAPI simulées.
JOB_ID = (
    "12345678-1234-5678-1234-567812345678"
)


# RÔLE :
# Simule httpx.Client.
#
# Cette classe :
#
# - fonctionne comme gestionnaire de contexte ;
# - conserve les appels request() ;
# - retourne une réponse configurée ;
# - peut lever une exception configurée ;
# - permet de vérifier sa fermeture.
class RecordingHttpClient:
    # RÔLE :
    # Initialise le faux client HTTP.
    #
    # REÇOIT :
    # - response :
    #   réponse HTTP retournée par request() ;
    #
    # - error :
    #   exception éventuellement levée par request().
    def __init__(
        self,
        *,
        response: httpx.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error

        self.calls: list[
            dict[str, Any]
        ] = []

        self.enter_count = 0
        self.exit_count = 0

    # RÔLE :
    # Simule l'ouverture du gestionnaire
    # de contexte httpx.Client.
    #
    # RETOURNE :
    # - self
    def __enter__(
        self,
    ) -> "RecordingHttpClient":
        self.enter_count += 1

        return self

    # RÔLE :
    # Simule la fermeture automatique
    # du client HTTP.
    #
    # RETOURNE :
    # - False afin de ne pas masquer
    #   les éventuelles exceptions.
    def __exit__(
        self,
        exception_type: object,
        exception_value: object,
        traceback: object,
    ) -> bool:
        self.exit_count += 1

        return False

    # RÔLE :
    # Simule httpx.Client.request().
    #
    # MODIFIE :
    # - self.calls
    #
    # RETOURNE :
    # - la réponse configurée.
    #
    # ERREUR :
    # - lève self.error lorsqu'elle existe.
    def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers"),
            }
        )

        if self.error is not None:
            raise self.error

        if self.response is None:
            raise AssertionError(
                "Le faux client HTTP ne contient "
                "ni réponse ni exception."
            )

        return self.response


# RÔLE :
# Construit une réponse HTTP JSON.
#
# APPELÉE PAR :
# - les tests de réponses FastAPI.
#
# RETOURNE :
# - httpx.Response avec une requête associée.
#
# IMPORTANT :
# httpx.Response.raise_for_status() exige
# la présence de response.request.
def _json_response(
    payload: Any,
    *,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        request=httpx.Request(
            "GET",
            "http://api.test/test",
        ),
    )


# RÔLE :
# Construit une réponse HTTP textuelle.
#
# UTILISÉE POUR :
# - JSON invalide ;
# - erreur HTTP sans JSON.
def _text_response(
    content: str,
    *,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=content.encode("utf-8"),
        request=httpx.Request(
            "GET",
            "http://api.test/test",
        ),
    )


# RÔLE :
# Remplace httpx.Client dans chat.services.
#
# APPELÉE PAR :
# - tous les tests exécutant une requête.
#
# RETOURNE :
# - le faux client ;
# - les arguments utilisés pour sa construction.
def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: httpx.Response | None = None,
    error: Exception | None = None,
) -> tuple[
    RecordingHttpClient,
    list[dict[str, Any]],
]:
    fake_client = RecordingHttpClient(
        response=response,
        error=error,
    )

    constructor_calls: list[
        dict[str, Any]
    ] = []

    # RÔLE :
    # Remplace le constructeur httpx.Client.
    #
    # REÇOIT :
    # - l'adresse FastAPI ;
    # - le timeout construit par services.py.
    def build_fake_client(
        *,
        base_url: str,
        timeout: httpx.Timeout,
    ) -> RecordingHttpClient:
        constructor_calls.append(
            {
                "base_url": base_url,
                "timeout": timeout,
            }
        )

        return fake_client

    monkeypatch.setattr(
        services.httpx,
        "Client",
        build_fake_client,
    )

    return fake_client, constructor_calls


# ---------------------------------------------------------------------------
# Construction du timeout
# ---------------------------------------------------------------------------


# Vérifie que les différentes phases HTTP
# utilisent les durées prévues.
def test_build_timeout_uses_django_settings() -> None:
    timeout = _build_timeout()

    assert timeout.connect == 5.0

    assert timeout.read == (
        services.settings
        .MYCODER_API_TIMEOUT_SECONDS
    )

    assert timeout.write == 10.0
    assert timeout.pool == 5.0


# ---------------------------------------------------------------------------
# Création d'un ticket
# ---------------------------------------------------------------------------


# Vérifie que create_job() envoie la conversation
# vers POST /v1/jobs puis analyse la réponse queued.
def test_create_job_sends_expected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "queued",
            "queue_position": 2,
            "queue_total": 4,
        },
        status_code=202,
    )

    fake_client, constructor_calls = (
        _install_fake_client(
            monkeypatch,
            response=response,
        )
    )

    history = [
        {
            "role": "user",
            "content": (
                "Explique le fonctionnement "
                "d'une frégate."
            ),
        }
    ]

    result = create_job(history)

    assert result == ApiJobCreated(
        job_id=JOB_ID,
        state="queued",
        queue_position=2,
        queue_total=4,
    )

    assert fake_client.calls == [
        {
            "method": "POST",
            "path": "/v1/jobs",
            "json": {
                "messages": history,
            },
            "headers": {
                "Accept": "application/json",
            },
        }
    ]

    assert len(constructor_calls) == 1

    assert (
        constructor_calls[0]["base_url"]
        == services.settings.MYCODER_API_BASE_URL
    )

    assert isinstance(
        constructor_calls[0]["timeout"],
        httpx.Timeout,
    )


# Vérifie que queue_total utilise zéro
# lorsque FastAPI omet exceptionnellement ce champ.
def test_create_job_defaults_missing_queue_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "queued",
            "queue_position": 1,
        },
        status_code=202,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    result = create_job(
        [
            {
                "role": "user",
                "content": "Bonjour.",
            }
        ]
    )

    assert result.queue_total == 0


# Vérifie qu'une réponse sans job_id
# ne peut pas être utilisée par Django.
def test_create_job_rejects_missing_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "state": "queued",
            "queue_position": 1,
            "queue_total": 1,
        },
        status_code=202,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="champ `job_id` valide",
    ):
        create_job(
            [
                {
                    "role": "user",
                    "content": "Bonjour.",
                }
            ]
        )


# Vérifie qu'un état FastAPI inconnu
# est refusé par le front.
def test_create_job_rejects_unknown_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "cancelled",
            "queue_position": None,
            "queue_total": 0,
        },
        status_code=202,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="état de job inconnu",
    ):
        create_job(
            [
                {
                    "role": "user",
                    "content": "Bonjour.",
                }
            ]
        )


# Vérifie que True n'est pas accepté
# comme position de file.
#
# En Python, bool hérite de int :
# cette protection doit donc être explicite.
def test_create_job_rejects_boolean_queue_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "queued",
            "queue_position": True,
            "queue_total": 1,
        },
        status_code=202,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="queue_position",
    ):
        create_job(
            [
                {
                    "role": "user",
                    "content": "Bonjour.",
                }
            ]
        )


# ---------------------------------------------------------------------------
# Lecture des états d'un ticket
# ---------------------------------------------------------------------------


# Vérifie la lecture d'un ticket queued.
def test_get_job_status_parses_queued_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "queued",
            "queue_position": 3,
            "queue_total": 5,
            "progress_percent": 0,
            "result": None,
            "error": None,
        }
    )

    fake_client, _ = _install_fake_client(
        monkeypatch,
        response=response,
    )

    result = get_job_status(JOB_ID)

    assert result == ApiJobStatus(
        job_id=JOB_ID,
        state="queued",
        queue_position=3,
        queue_total=5,
        progress_percent=0,
        content=None,
        model=None,
        error=None,
    )

    assert fake_client.calls[0]["method"] == "GET"

    assert fake_client.calls[0]["path"] == (
        f"/v1/jobs/{JOB_ID}"
    )


# Vérifie la lecture d'un ticket running
# avec une progression indéterminée.
def test_get_job_status_parses_running_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "running",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": None,
            "result": None,
            "error": None,
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    result = get_job_status(JOB_ID)

    assert result.state == "running"
    assert result.queue_position is None
    assert result.progress_percent is None
    assert result.content is None
    assert result.model is None
    assert result.error is None


# Vérifie la lecture d'un ticket completed
# et l'extraction de son résultat.
def test_get_job_status_parses_completed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "completed",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": 100,
            "result": {
                "content": (
                    "  Réponse finale de Qwen.  "
                ),
                "model": (
                    "  qwen-test-model  "
                ),
            },
            "error": None,
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    result = get_job_status(JOB_ID)

    assert result.state == "completed"
    assert result.progress_percent == 100

    assert result.content == (
        "Réponse finale de Qwen."
    )

    assert result.model == "qwen-test-model"
    assert result.error is None


# Vérifie la lecture d'un ticket failed
# et l'extraction du message d'erreur.
def test_get_job_status_parses_failed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "failed",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": None,
            "result": None,
            "error": (
                "  Ollama est indisponible.  "
            ),
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    result = get_job_status(JOB_ID)

    assert result.state == "failed"

    assert result.error == (
        "Ollama est indisponible."
    )

    assert result.content is None
    assert result.model is None
    assert result.progress_percent is None


# ---------------------------------------------------------------------------
# Validation des champs retournés par FastAPI
# ---------------------------------------------------------------------------


# Vérifie que result doit être
# un dictionnaire ou None.
def test_get_job_status_rejects_invalid_result_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "completed",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": 100,
            "result": "résultat invalide",
            "error": None,
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="champ `result` invalide",
    ):
        get_job_status(JOB_ID)


# Vérifie qu'un résultat présent
# doit indiquer le modèle utilisé.
def test_get_job_status_rejects_result_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "completed",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": 100,
            "result": {
                "content": "Réponse valide.",
            },
            "error": None,
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="champ `model` valide",
    ):
        get_job_status(JOB_ID)


# Vérifie que True ne peut pas être interprété
# comme une progression entière.
def test_get_job_status_rejects_boolean_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "running",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": True,
            "result": None,
            "error": None,
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="progress_percent",
    ):
        get_job_status(JOB_ID)


# Vérifie qu'un nombre négatif
# de tickets queued est refusé.
def test_get_job_status_rejects_negative_queue_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "running",
            "queue_position": None,
            "queue_total": -1,
            "progress_percent": None,
            "result": None,
            "error": None,
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="queue_total",
    ):
        get_job_status(JOB_ID)


# Vérifie qu'une erreur composée seulement
# d'espaces devient None.
def test_get_job_status_normalises_blank_error_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "job_id": JOB_ID,
            "state": "running",
            "queue_position": None,
            "queue_total": 0,
            "progress_percent": None,
            "result": None,
            "error": "   ",
        }
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    result = get_job_status(JOB_ID)

    assert result.error is None


# ---------------------------------------------------------------------------
# Erreurs réseau et timeout
# ---------------------------------------------------------------------------


# Vérifie qu'un dépassement de délai httpx
# devient une ApiClientError compréhensible.
def test_request_json_translates_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request(
        "GET",
        "http://api.test/slow",
    )

    error = httpx.ReadTimeout(
        "Délai dépassé.",
        request=request,
    )

    fake_client, _ = _install_fake_client(
        monkeypatch,
        error=error,
    )

    with pytest.raises(
        ApiClientError,
        match="trop de temps",
    ):
        _request_json(
            "GET",
            "/slow",
        )

    assert fake_client.exit_count == 1


# Vérifie qu'une connexion refusée
# devient une ApiClientError indiquant FastAPI.
def test_request_json_translates_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request(
        "GET",
        "http://api.test/unavailable",
    )

    error = httpx.ConnectError(
        "Connexion refusée.",
        request=request,
    )

    _install_fake_client(
        monkeypatch,
        error=error,
    )

    with pytest.raises(
        ApiClientError,
        match="Impossible de joindre FastAPI",
    ):
        _request_json(
            "GET",
            "/unavailable",
        )


# ---------------------------------------------------------------------------
# Erreurs HTTP
# ---------------------------------------------------------------------------


# Vérifie que le champ FastAPI detail
# est conservé dans l'erreur Django.
def test_request_json_extracts_http_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "detail": (
                "RabbitMQ est indisponible."
            ),
        },
        status_code=503,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="RabbitMQ est indisponible",
    ):
        _request_json(
            "POST",
            "/v1/jobs",
            json_body={
                "messages": [],
            },
        )


# Vérifie le message utilisé lorsqu'une erreur HTTP
# ne contient pas de JSON lisible.
def test_request_json_handles_non_json_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _text_response(
        "Service indisponible",
        status_code=502,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="sans contenu JSON lisible",
    ):
        _request_json(
            "GET",
            "/broken",
        )


# Vérifie le message générique lorsqu'une erreur HTTP
# contient du JSON mais aucun champ detail exploitable.
def test_request_json_handles_http_error_without_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "message": "Erreur interne.",
        },
        status_code=500,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="erreur HTTP 500",
    ):
        _request_json(
            "GET",
            "/broken",
        )


# ---------------------------------------------------------------------------
# Réponses JSON invalides
# ---------------------------------------------------------------------------


# Vérifie qu'une réponse HTTP 200
# contenant du texte non JSON est refusée.
def test_request_json_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _text_response(
        "ceci-n-est-pas-du-json",
        status_code=200,
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="réponse JSON valide",
    ):
        _request_json(
            "GET",
            "/invalid-json",
        )


# Vérifie que la racine JSON doit être
# un objet et non une liste.
def test_request_json_rejects_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        [
            {
                "status": "ok",
            }
        ]
    )

    _install_fake_client(
        monkeypatch,
        response=response,
    )

    with pytest.raises(
        ApiClientError,
        match="structure attendue",
    ):
        _request_json(
            "GET",
            "/list-response",
        )


# ---------------------------------------------------------------------------
# Fermeture du client
# ---------------------------------------------------------------------------


# Vérifie que le gestionnaire de contexte
# ouvre et ferme toujours le client HTTP.
def test_request_json_closes_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {
            "status": "ok",
        }
    )

    fake_client, _ = _install_fake_client(
        monkeypatch,
        response=response,
    )

    payload = _request_json(
        "GET",
        "/health",
    )

    assert payload == {
        "status": "ok",
    }

    assert fake_client.enter_count == 1
    assert fake_client.exit_count == 1