import os
from pathlib import Path

# Chemin absolu vers le dossier principal du front Django.
BASE_DIR = Path(__file__).resolve().parent.parent

# Clé utilisée par Django pour signer les sessions et autres données sensibles.
# Elle est récupérée depuis l'environnement afin de ne pas être écrite en dur.
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "unsafe-local-development-key",
)

# Active ou désactive le mode de développement selon la variable d'environnement.
DEBUG = os.getenv(
    "DJANGO_DEBUG",
    "1",
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Liste des noms de domaine et adresses autorisés à accéder à l'application.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "DJANGO_ALLOWED_HOSTS",
        "127.0.0.1,localhost",
    ).split(",")
    if host.strip()
]

# Applications Django chargées au démarrage du projet.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "chat.apps.ChatConfig",
]

# Composants exécutés autour de chaque requête HTTP
# pour gérer la sécurité, les sessions, l'authentification et les messages.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Fichier principal contenant les routes du projet Django.
ROOT_URLCONF = "config.urls"

# Configuration du moteur de templates utilisé pour générer les pages HTML.
TEMPLATES = [
    {
        "BACKEND": (
            "django.template.backends.django."
            "DjangoTemplates"
        ),
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                (
                    "django.template.context_processors."
                    "request"
                ),
                (
                    "django.contrib.auth.context_processors."
                    "auth"
                ),
                (
                    "django.contrib.messages.context_processors."
                    "messages"
                ),
            ],
        },
    },
]

# Point d'entrée WSGI utilisé par les serveurs web compatibles.
WSGI_APPLICATION = "config.wsgi.application"

# Configuration de la base SQLite utilisée localement
# pour les sessions et les données Django.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Validateurs de mots de passe, laissés vides puisque
# l'application ne gère pas encore de comptes utilisateurs.
AUTH_PASSWORD_VALIDATORS = []

# Paramètres régionaux utilisés par Django pour les textes,
# les dates et les heures.
LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"

USE_I18N = True
USE_TZ = True

# Préfixe URL utilisé pour servir les fichiers CSS, JavaScript et images.
STATIC_URL = "static/"

# Type de clé primaire utilisé par défaut pour les futurs modèles Django.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Configure une durée maximale de douze heures
# et ferme la session lorsque le navigateur est fermé.
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# Adresse de l'API FastAPI appelée par le serveur Django.
MYCODER_API_BASE_URL = os.getenv(
    "MYCODER_API_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

# Durée maximale pendant laquelle Django attend une réponse générée.
# Elle est légèrement supérieure au délai maximal du client Ollama.
MYCODER_API_TIMEOUT_SECONDS = float(
    os.getenv(
        "MYCODER_API_TIMEOUT_SECONDS",
        "330",
    )
)