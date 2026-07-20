from local_qwen_worker.prompts import DEFAULT_SYSTEM_PROMPT
from local_qwen_worker.service import QwenService

from .schemas import ChatRequest, ChatResponse


# Crée une instance partagée du service Qwen.
# FastAPI utilisera cette instance pour transmettre les conversations
# au worker sans exposer directement Ollama au front Django.
_qwen_service = QwenService()


# Convertit la requête validée par FastAPI en messages compris par le worker,
# ajoute le prompt système contrôlé par l'application puis lance la génération.
def generate_chat_response(
    request: ChatRequest,
) -> ChatResponse:
    messages = [
        message.model_dump()
        for message in request.messages
    ]

    result = _qwen_service.chat(
        messages=messages,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )

    return ChatResponse(
        content=result.content,
        model=result.model,
    )