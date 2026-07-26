# Documentation MyCoder

Ce dossier décrit l’architecture, les composants et les pipelines
de l’application MyCoder.

## Architecture générale

- [Architecture globale](architecture.md)

## Pipelines

- [Démarrage de l’application](pipelines/startup.md)
- [Création et traitement d’un job](pipelines/chat-job.md)

## Composants

- [Front Django](components/front-django.md)
- [API FastAPI](components/api-fastapi.md)
- [RabbitMQ](components/rabbitmq.md)
- [Worker Qwen](components/worker-qwen.md)

## Conventions

- [Commentaires et navigation dans le code](conventions/comments.md)

## Dépannage

- [Guide de dépannage](troubleshooting.md)

## Principaux identifiants de pipeline

| Identifiant | Description |
|---|---|
| `STARTUP` | Démarrage de l’infrastructure locale |
| `CHAT_PAGE_DISPLAY` | Affichage de la conversation |
| `CHAT_JOB_CREATE` | Création d’un ticket |
| `CHAT_JOB_PUBLISH` | Publication dans RabbitMQ |
| `CHAT_JOB_CONSUME` | Récupération par le worker |
| `CHAT_JOB_GENERATE` | Génération Ollama/Qwen |
| `CHAT_JOB_EVENT` | Publication d’un changement d’état |
| `CHAT_JOB_STATUS` | Suivi périodique du ticket |
| `CHAT_JOB_COMPLETE` | Enregistrement de la réponse |
| `CHAT_JOB_FAILURE` | Gestion d’un échec |
| `CHAT_CLEAR` | Suppression de la conversation locale |