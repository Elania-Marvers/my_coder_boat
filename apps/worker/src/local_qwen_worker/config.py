from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Regroupe et valide tous les paramètres nécessaires au worker :
# modèle Qwen, contexte, température, adresse Ollama et délais d'attente.
class WorkerSettings(BaseSettings):
    # Indique à Pydantic de charger automatiquement les valeurs
    # depuis le fichier .env et d'ignorer les variables inconnues.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Paramètres contrôlant le modèle local et son comportement de génération.
    qwen_model: str = "qwen2.5-coder:7b-instruct-q4_K_M"
    qwen_context: int = 8192
    qwen_temperature: float = 0.25

    # Paramètres de connexion au serveur Ollama local.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_timeout_seconds: float = 300.0
    ollama_keep_alive: str = "10m"

# Charge les paramètres une seule fois puis réutilise
# la même instance pendant toute la durée du programme.
@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    return WorkerSettings()
