from fastapi import FastAPI, HTTPException, status

from local_qwen_worker.ollama_client import (
    OllamaRequestError,
    OllamaUnavailableError,
)

from .schemas import ChatRequest, ChatResponse
from .services import generate_chat_response


# Crée l'application FastAPI qui coordonne désormais
# les appels entre le front Django et le worker Qwen.
app = FastAPI(
    title="MyCoder API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


# Vérifie que le serveur FastAPI est démarré.
# Cette route ne contacte ni le worker ni Ollama.
@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "mycoder-api",
    }


# Reçoit une conversation depuis Django, la transmet au worker
# puis retourne le texte généré et le modèle utilisé.
#
# La fonction reste synchrone car le client Ollama actuel est synchrone.
# FastAPI exécutera cette route dans son pool de threads.
@app.post(
    "/v1/chat",
    response_model=ChatResponse,
)
def chat(request: ChatRequest) -> ChatResponse:
    try:
        return generate_chat_response(request)

    # Retourne 503 lorsque le serveur Ollama local
    # ne peut pas être contacté.
    except OllamaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    # Retourne 502 lorsqu'Ollama répond mais refuse la demande,
    # par exemple lorsque le modèle demandé n'est pas installé.
    except OllamaRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    # Retourne 400 lorsqu'une erreur fonctionnelle est détectée,
    # par exemple une conversation finalement inutilisable.
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc