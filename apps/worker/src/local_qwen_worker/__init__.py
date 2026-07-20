# Importe les principaux objets du worker afin de les rendre
# accessibles directement depuis le package local_qwen_worker.
from .ollama_client import (
    OllamaRequestError,
    OllamaUnavailableError,
    QwenWorkerError,
)
from .schemas import ChatMessage, ChatResult
from .service import QwenService

# Définit explicitement les classes et types constituant
# l'interface publique du package.
__all__ = [
    "ChatMessage",
    "ChatResult",
    "OllamaRequestError",
    "OllamaUnavailableError",
    "QwenService",
    "QwenWorkerError",
]
