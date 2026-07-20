SHELL := /bin/zsh

# Charge automatiquement les variables définies dans le fichier .env.
# Le tiret devant include évite une erreur si le fichier n'existe pas encore.
-include .env
export


# Version de Python utilisée par le workspace.
PYTHON_VERSION ?= 3.12


# Modèle Qwen utilisé par Ollama.
QWEN_MODEL ?= qwen2.5-coder:7b-instruct-q4_K_M

# Taille maximale du contexte transmis au modèle.
QWEN_CONTEXT ?= 8192


# Adresse du serveur Ollama local.
OLLAMA_BASE_URL ?= http://127.0.0.1:11434


# Adresse et port du front Django.
FRONT_HOST ?= 127.0.0.1
FRONT_PORT ?= 5635


# Adresse et port de l'API FastAPI.
API_HOST ?= 127.0.0.1
API_PORT ?= 8000


# Emplacement du fichier de dump local non versionné.
DUMP_FILE ?= .local/project_dump.txt


# Déclare les commandes qui ne correspondent pas à des fichiers.
.PHONY: setup
.PHONY: sync
.PHONY: front-bootstrap
.PHONY: migrate
.PHONY: front-check
.PHONY: front-test
.PHONY: front-import-check
.PHONY: front
.PHONY: api
.PHONY: api-check
.PHONY: ollama-check
.PHONY: model-pull
.PHONY: qwen
.PHONY: dev
.PHONY: test
.PHONY: lint
.PHONY: dump
.PHONY: tree
.PHONY: clean


# Prépare complètement le projet :
# dépendances, structure Django, migrations et vérification du front.
setup:
	@$(MAKE) sync
	@$(MAKE) front-bootstrap
	@$(MAKE) migrate
	@$(MAKE) front-check
	@echo "Installation terminée."


# Installe Python puis synchronise les dépendances
# du front, de l'API, du worker et des outils de développement.
sync:
	@uv python install "$(PYTHON_VERSION)"
	@uv sync --all-packages --all-groups


# Complète les fichiers manquants de l'application Django
# sans écraser les fichiers déjà présents.
front-bootstrap:
	@uv run python scripts/bootstrap_front.py


# Applique les migrations Django nécessaires,
# notamment celles utilisées pour les sessions.
migrate:
	@uv run --project apps/front \
		python apps/front/manage.py migrate


# Vérifie la configuration générale du projet Django.
front-check:
	@uv run --project apps/front \
		python apps/front/manage.py check


# Exécute les tests automatiques de l'application Django chat.
front-test:
	@uv run --project apps/front \
		python apps/front/manage.py test chat


# Vérifie que Django peut importer correctement
# la configuration de l'application chat.
front-import-check:
	@uv run --project apps/front \
		python apps/front/manage.py shell \
		-c "from chat.apps import ChatConfig; print(ChatConfig.name)"


# Lance uniquement le serveur de développement Django.
front:
	@uv run --project apps/front \
		python apps/front/manage.py runserver \
		"$(FRONT_HOST):$(FRONT_PORT)"


# Lance uniquement l'API FastAPI avec rechargement automatique.
# Cette commande est pratique lorsque tu travailles seulement sur l'API.
api:
	@uv run --project apps/api \
		uvicorn app.main:app \
		--app-dir apps/api \
		--reload \
		--host "$(API_HOST)" \
		--port "$(API_PORT)"


# Vérifie que l'API FastAPI répond sur sa route de santé.
api-check:
	@curl \
		--fail \
		--silent \
		--show-error \
		"http://$(API_HOST):$(API_PORT)/health" \
		>/dev/null
	@echo "API FastAPI disponible sur http://$(API_HOST):$(API_PORT)."


# Vérifie qu'Ollama est installé et joignable.
# Si l'application ne répond pas, la commande tente de la démarrer.
ollama-check:
	@command -v ollama >/dev/null || { \
		echo "Ollama n'est pas installé."; \
		echo "Commande : brew install --cask ollama-app"; \
		exit 1; \
	}
	@if ! curl \
		--fail \
		--silent \
		"$(OLLAMA_BASE_URL)/api/tags" \
		>/dev/null 2>&1; then \
		echo "Démarrage de l'application Ollama..."; \
		open -a Ollama; \
		sleep 5; \
	fi
	@curl \
		--fail \
		--silent \
		"$(OLLAMA_BASE_URL)/api/tags" \
		>/dev/null || { \
		echo "Ollama ne répond pas sur $(OLLAMA_BASE_URL)."; \
		exit 1; \
	}
	@echo "Ollama disponible sur $(OLLAMA_BASE_URL)."


# Télécharge dans Ollama le modèle défini par QWEN_MODEL.
model-pull:
	@$(MAKE) ollama-check
	@ollama pull "$(QWEN_MODEL)"


# Ouvre une conversation directe avec Qwen dans le terminal.
# Cette commande ne passe ni par Django ni par FastAPI.
qwen:
	@$(MAKE) ollama-check
	@ollama run "$(QWEN_MODEL)"


# Démarre toute l'architecture locale :
# 1. vérifie Ollama ;
# 2. démarre FastAPI en arrière-plan ;
# 3. attend que FastAPI soit disponible ;
# 4. démarre Django au premier plan.
#
# Lorsque Django est arrêté avec Ctrl+C,
# le processus FastAPI lancé ici est également arrêté.
dev:
	@set -e; \
	$(MAKE) ollama-check; \
	echo "Démarrage de FastAPI..."; \
	uv run --project apps/api \
		uvicorn app.main:app \
		--app-dir apps/api \
		--host "$(API_HOST)" \
		--port "$(API_PORT)" & \
	API_PID=$$!; \
	trap 'echo "Arrêt de FastAPI..."; kill "$$API_PID" 2>/dev/null || true' EXIT INT TERM; \
	echo "Attente de la disponibilité de FastAPI..."; \
	API_READY=0; \
	for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do \
		if curl \
			--fail \
			--silent \
			"http://$(API_HOST):$(API_PORT)/health" \
			>/dev/null 2>&1; then \
			API_READY=1; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if [ "$$API_READY" -ne 1 ]; then \
		echo "FastAPI n'a pas démarré correctement."; \
		exit 1; \
	fi; \
	echo "FastAPI disponible sur http://$(API_HOST):$(API_PORT)."; \
	echo "Démarrage de Django sur http://$(FRONT_HOST):$(FRONT_PORT)..."; \
	uv run --project apps/front \
		python apps/front/manage.py runserver \
		"$(FRONT_HOST):$(FRONT_PORT)"


# Lance tous les tests Python du workspace avec pytest.
test:
	@uv run pytest


# Analyse le code Python avec Ruff.
lint:
	@uv run ruff check .


# Génère un fichier texte regroupant les fichiers utiles
# du projet tout en excluant les secrets et fichiers locaux.
dump:
	@uv run python scripts/dump_project.py \
		--output "$(DUMP_FILE)"


# Affiche l'arborescence principale du projet
# sans les environnements ni les fichiers locaux.
tree:
	@find . \
		-maxdepth 5 \
		-not -path "./.venv/*" \
		-not -path "./.git/*" \
		-not -path "./.local/*" \
		| sort


# Supprime les caches Python, pytest et Ruff.
clean:
	@find . \
		-type d \
		-name "__pycache__" \
		-prune \
		-exec rm -rf {} + \
		2>/dev/null || true
	@rm -rf .pytest_cache
	@rm -rf .ruff_cache
