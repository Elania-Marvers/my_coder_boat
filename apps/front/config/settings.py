"""
FICHIER :
apps/front/config/settings.py

RÔLE GÉNÉRAL :
Configure le projet Django servant d'interface web
à l'application MyCoder.

Ce fichier centralise notamment :

- la sécurité Django ;
- les applications installées ;
- les middlewares ;
- les templates ;
- la base SQLite locale ;
- les sessions du navigateur ;
- les fichiers statiques ;
- la communication HTTP avec FastAPI.

CIRCULATION PRINCIPALE :

Navigateur
→ Django
→ apps/front/chat/views.py
→ apps/front/chat/services.py
→ FastAPI

IMPORTANT :
Django n'attend plus directement toute la génération Qwen.

Les appels vers FastAPI sont désormais courts :

1. création d'un ticket ;
2. lecture régulière de son état.

La génération longue est effectuée séparément par :

RabbitMQ
→ worker
→ Ollama

UTILISÉ PAR :
- apps/front/manage.py
- apps/front/config/wsgi.py
- apps/front/config/asgi.py
- toutes les applications Django

PIPELINES :
- STARTUP
- CHAT_PAGE_DISPLAY
- CHAT_JOB_CREATE
- CHAT_JOB_STATUS
- CHAT_JOB_COMPLETE
- CHAT_JOB_FAILURE
"""

import os
from pathlib import Path
from urllib.parse import urlparse


# Chemin absolu vers le dossier apps/front.
#
# UTILISÉ PAR :
# - DATABASES
# - futures configurations de fichiers statiques
# - futurs dossiers de médias
BASE_DIR = Path(__file__).resolve().parent.parent


# Valeur locale de secours utilisée uniquement
# lorsque Django fonctionne en mode développement.
UNSAFE_DEVELOPMENT_SECRET_KEY = (
    "unsafe-local-development-key"
)


# RÔLE :
# Lit une variable d'environnement booléenne.
#
# APPELÉE PAR :
# - la définition de DEBUG
#
# VALEURS VRAIES :
# - 1
# - true
# - yes
# - on
#
# VALEURS FAUSSES :
# - 0
# - false
# - no
# - off
#
# ERREUR :
# - ValueError lorsque la valeur n'est pas reconnue.
#
# PIPELINE :
# - STARTUP
def _read_boolean_environment_variable(
    name: str,
    *,
    default: bool,
) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    clean_value = raw_value.strip().lower()

    if clean_value in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    if clean_value in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False

    raise ValueError(
        f"La variable {name} doit contenir "
        "une valeur booléenne reconnue."
    )


# RÔLE :
# Lit une variable contenant plusieurs valeurs
# séparées par des virgules.
#
# APPELÉE PAR :
# - la définition de ALLOWED_HOSTS
#
# EXEMPLE :
# 127.0.0.1,localhost,mycoder.example
#
# RETOURNE :
# - Une liste sans valeurs vides ;
# - la valeur par défaut lorsque la variable est absente.
#
# PIPELINE :
# - STARTUP
def _read_list_environment_variable(
    name: str,
    *,
    default: str,
) -> list[str]:
    raw_value = os.getenv(
        name,
        default,
    )

    return [
        item.strip()
        for item in raw_value.split(",")
        if item.strip()
    ]


# RÔLE :
# Lit une durée positive exprimée en secondes.
#
# APPELÉE PAR :
# - la définition de MYCODER_API_TIMEOUT_SECONDS
#
# RETOURNE :
# - Une valeur float strictement positive.
#
# ERREURS :
# - ValueError si la valeur n'est pas numérique ;
# - ValueError si la valeur est nulle ou négative.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _read_positive_float_environment_variable(
    name: str,
    *,
    default: float,
) -> float:
    raw_value = os.getenv(
        name,
        str(default),
    )

    try:
        value = float(raw_value)

    except ValueError as exc:
        raise ValueError(
            f"La variable {name} doit contenir "
            "un nombre valide."
        ) from exc

    if value <= 0:
        raise ValueError(
            f"La variable {name} doit être "
            "strictement supérieure à zéro."
        )

    return value


# RÔLE :
# Lit et valide une adresse HTTP utilisée
# par le serveur Django.
#
# APPELÉE PAR :
# - la définition de MYCODER_API_BASE_URL
#
# VÉRIFIE :
# - le protocole http ou https ;
# - la présence d'un nom d'hôte ou d'une adresse IP.
#
# RETOURNE :
# - L'adresse sans slash final.
#
# PIPELINES :
# - CHAT_JOB_CREATE
# - CHAT_JOB_STATUS
def _read_http_url_environment_variable(
    name: str,
    *,
    default: str,
) -> str:
    value = os.getenv(
        name,
        default,
    ).strip().rstrip("/")

    parsed_url = urlparse(value)

    if parsed_url.scheme not in {
        "http",
        "https",
    }:
        raise ValueError(
            f"La variable {name} doit commencer "
            "par http:// ou https://."
        )

    if not parsed_url.netloc:
        raise ValueError(
            f"La variable {name} doit contenir "
            "un hôte valide."
        )

    return value


# Active le mode de développement Django.
#
# VARIABLE :
# DJANGO_DEBUG
#
# UTILISÉ PAR :
# - Django pour les messages d'erreur ;
# - le serveur de fichiers statiques ;
# - différentes protections de production.
DEBUG = _read_boolean_environment_variable(
    "DJANGO_DEBUG",
    default=True,
)


# Clé utilisée par Django pour :
#
# - signer les sessions ;
# - protéger certains jetons ;
# - signer différentes données internes.
#
# VARIABLE :
# DJANGO_SECRET_KEY
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    UNSAFE_DEVELOPMENT_SECRET_KEY,
).strip()


# Empêche l'utilisation accidentelle de la clé locale
# lorsque Django est lancé comme une application de production.
if (
    not DEBUG
    and SECRET_KEY
    == UNSAFE_DEVELOPMENT_SECRET_KEY
):
    raise ValueError(
        "DJANGO_SECRET_KEY doit être définie "
        "avec une valeur sécurisée lorsque "
        "DJANGO_DEBUG est désactivé."
    )


# Liste des hôtes autorisés à contacter Django.
#
# VARIABLE :
# DJANGO_ALLOWED_HOSTS
#
# VALEUR LOCALE PAR DÉFAUT :
# - 127.0.0.1
# - localhost
ALLOWED_HOSTS = (
    _read_list_environment_variable(
        "DJANGO_ALLOWED_HOSTS",
        default="127.0.0.1,localhost",
    )
)


# Applications chargées par Django.
#
# chat.apps.ChatConfig correspond à :
# apps/front/chat/apps.py::ChatConfig
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "chat.apps.ChatConfig",
]


# Composants exécutés autour de chaque requête HTTP.
#
# Ils gèrent notamment :
#
# - la sécurité ;
# - les sessions ;
# - les URL ;
# - la protection CSRF ;
# - l'authentification ;
# - les messages Django ;
# - la protection contre l'affichage en iframe.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    (
        "django.contrib.auth.middleware."
        "AuthenticationMiddleware"
    ),
    (
        "django.contrib.messages.middleware."
        "MessageMiddleware"
    ),
    (
        "django.middleware.clickjacking."
        "XFrameOptionsMiddleware"
    ),
]


# Fichier principal contenant les routes
# générales du projet Django.
#
# POINTE VERS :
# apps/front/config/urls.py
ROOT_URLCONF = "config.urls"


# Configuration du moteur de templates.
#
# APP_DIRS=True autorise Django à rechercher :
#
# apps/front/chat/templates/chat/index.html
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
                    "django.contrib.messages."
                    "context_processors.messages"
                ),
            ],
        },
    },
]


# Points d'entrée des serveurs Django.
#
# WSGI est adapté aux serveurs synchrones classiques.
# ASGI permettra plus tard l'utilisation de WebSocket
# ou d'autres communications asynchrones.
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# Base SQLite locale.
#
# UTILISÉE ACTUELLEMENT POUR :
# - les sessions Django ;
# - les tables internes de Django.
#
# Le contenu des jobs FastAPI n'est pas stocké ici.
DATABASES = {
    "default": {
        "ENGINE": (
            "django.db.backends.sqlite3"
        ),
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


# Aucun système de comptes utilisateur n'est encore exposé.
#
# Les validateurs seront complétés lorsqu'une authentification
# réelle sera ajoutée au projet.
AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []


# Configuration linguistique et temporelle.
LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"

USE_I18N = True
USE_TZ = True


# Préfixe HTTP des fichiers statiques.
#
# UTILISÉ POUR :
# - chat.css
# - chat.js
# - favicon.svg
STATIC_URL = "/static/"


# Type de clé primaire créé par défaut
# pour les futurs modèles Django.
DEFAULT_AUTO_FIELD = (
    "django.db.models.BigAutoField"
)


# Durée maximale de la session navigateur :
# douze heures.
SESSION_COOKIE_AGE = 60 * 60 * 12


# La session est supprimée lorsque
# le navigateur est complètement fermé.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True


# Empêche le JavaScript du navigateur
# de lire directement le cookie de session.
SESSION_COOKIE_HTTPONLY = True


# Limite l'envoi du cookie de session
# aux navigations compatibles avec SameSite=Lax.
SESSION_COOKIE_SAMESITE = "Lax"


# Adresse de FastAPI appelée par :
#
# apps/front/chat/services.py::_request_json()
#
# VARIABLE :
# MYCODER_API_BASE_URL
MYCODER_API_BASE_URL = (
    _read_http_url_environment_variable(
        "MYCODER_API_BASE_URL",
        default="http://127.0.0.1:8000",
    )
)


# Durée maximale d'un appel HTTP court
# entre Django et FastAPI.
#
# VARIABLE :
# MYCODER_API_TIMEOUT_SECONDS
#
# UTILISÉE PAR :
# apps/front/chat/services.py::_build_timeout()
#
# IMPORTANT :
# Cette durée ne correspond pas à la génération Qwen.
#
# Django effectue seulement :
#
# - POST /v1/jobs pour créer le ticket ;
# - GET /v1/jobs/{job_id} pour lire son état.
#
# La génération elle-même continue séparément
# dans le worker RabbitMQ.
MYCODER_API_TIMEOUT_SECONDS = (
    _read_positive_float_environment_variable(
        "MYCODER_API_TIMEOUT_SECONDS",
        default=15.0,
    )
)