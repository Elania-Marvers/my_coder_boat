from collections.abc import Sequence

from ollama import Client, ResponseError

from .config import WorkerSettings, get_settings
from .schemas import ChatMessage, ChatResult

# Classe de base pour toutes les erreurs prévues du worker.
# Ces erreurs peuvent être présentées proprement dans l'interface.
class QwenWorkerError(RuntimeError):
    """Erreur contrôlée pouvant être affichée dans le front."""

# Signale que le serveur Ollama local ne répond pas
# ou ne peut pas être contacté.
class OllamaUnavailableError(QwenWorkerError):
    """Ollama n'est pas joignable."""

# Signale qu'Ollama a reçu la demande mais l'a refusée,
# ou qu'il a retourné une réponse inutilisable.
class OllamaRequestError(QwenWorkerError):
    """Ollama a refusé la requête ou renvoyé une réponse invalide."""

# Encapsule le client officiel Ollama et centralise
# toutes les communications avec le modèle local.
class OllamaClient:

    # Initialise le client Ollama avec les paramètres fournis
    # ou avec la configuration chargée depuis l'environnement.
    def __init__(self, settings: WorkerSettings | None = None) -> None:
        self.settings = settings or get_settings()

        self.client = Client(
            host=self.settings.ollama_base_url,
            timeout=self.settings.ollama_timeout_seconds,
        )

    # Envoie une liste de messages au modèle Qwen via Ollama.
    # Configure les paramètres de génération, intercepte les erreurs
    # et retourne une réponse validée sous la forme d'un ChatResult.
    def chat(self, messages: Sequence[ChatMessage]) -> ChatResult:
        try:
            # Convertit les objets Pydantic en dictionnaires puis lance
            # une génération non diffusée avec le modèle configuré.
            response = self.client.chat(
                model=self.settings.qwen_model,
                messages=[
                    message.model_dump()
                    for message in messages
                ],
                stream=False,
                options={
                    "temperature": self.settings.qwen_temperature,
                    "num_ctx": self.settings.qwen_context,
                },
                keep_alive=self.settings.ollama_keep_alive,
            )

        # Traite les erreurs HTTP explicitement renvoyées par Ollama.
        except ResponseError as exc:
            # Une erreur 404 indique généralement que le modèle demandé
            # n'a pas encore été téléchargé dans Ollama.
            if getattr(exc, "status_code", None) == 404:
                raise OllamaRequestError(
                    f"Le modèle {self.settings.qwen_model} n'est pas installé. "
                    "Lance `make model-pull`."
                ) from exc

            detail = getattr(exc, "error", str(exc))

            raise OllamaRequestError(
                f"Ollama a refusé la requête : {detail}"
            ) from exc

        # Convertit les erreurs de connexion ou les problèmes inattendus
        # en une erreur spécifique indiquant qu'Ollama est indisponible.
        except Exception as exc:
            raise OllamaUnavailableError(
                "Impossible de joindre Ollama. Vérifie que l'application "
                "Ollama est ouverte, puis lance `make ollama-check`."
            ) from exc

        # Récupère le texte de la réponse et retire les espaces superflus.
        content = response.message.content.strip()

        # Refuse une réponse vide afin que le front n'affiche pas
        # un message assistant sans contenu.
        if not content:
            raise OllamaRequestError(
                "Ollama a renvoyé une réponse vide."
            )

        # Retourne un objet standard contenant le texte généré
        # et le nom du modèle réellement utilisé.
        return ChatResult(
            content=content,
            model=(
                getattr(response, "model", None)
                or self.settings.qwen_model
            ),
        )
