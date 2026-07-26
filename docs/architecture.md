# Architecture de MyCoder

## Objectif

MyCoder fournit une interface locale permettant d’envoyer une conversation
à un modèle Qwen exécuté avec Ollama.

Le traitement est asynchrone : le navigateur ne reste pas bloqué pendant
la génération.

## Composants

### Front Django

Le front :

- affiche la conversation ;
- valide le formulaire ;
- demande à FastAPI de créer un ticket ;
- stocke l’identifiant actif dans la session ;
- interroge régulièrement l’état du ticket ;
- ajoute la réponse finale dans l’historique.

### API FastAPI

L’API :

- crée un identifiant UUID ;
- conserve l’état du ticket en mémoire ;
- publie le travail dans RabbitMQ ;
- consomme les événements du worker ;
- expose l’état du ticket au front.

### RabbitMQ

RabbitMQ contient deux files :

- `mycoder.jobs` : demandes à traiter ;
- `mycoder.events` : états et résultats produits par le worker.

### Worker

Le worker :

- consomme un seul ticket à la fois ;
- publie l’état `running` ;
- appelle QwenService ;
- appelle Ollama ;
- publie `completed` ou `failed`.

### Ollama

Ollama charge et exécute le modèle local :

`qwen2.5-coder:7b-instruct-q4_K_M`

## Vue générale

```mermaid
flowchart LR
    Browser[Navigateur]
    Django[Front Django]
    API[API FastAPI]
    Jobs[RabbitMQ mycoder.jobs]
    Worker[Worker Qwen]
    Ollama[Ollama]
    Events[RabbitMQ mycoder.events]

    Browser --> Django
    Django --> API
    API --> Jobs
    Jobs --> Worker
    Worker --> Ollama
    Ollama --> Worker
    Worker --> Events
    Events --> API
    Django --> API
    API --> Django
    Django --> Browser