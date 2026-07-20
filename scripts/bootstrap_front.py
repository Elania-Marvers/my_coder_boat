from __future__ import annotations

from pathlib import Path

# Détermine automatiquement la racine du projet à partir
# de l'emplacement actuel du script.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Chemin vers l'application Django chat à initialiser.
CHAT_DIR = PROJECT_ROOT / "apps" / "front" / "chat"

# Liste les fichiers minimaux nécessaires à l'application Django
# ainsi que leur contenu initial lorsqu'ils sont absents.
FILES: dict[Path, str] = {
    CHAT_DIR / "__init__.py": "",
    CHAT_DIR / "apps.py": """from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chat"
    verbose_name = "MyCoder Chat"
""",
    CHAT_DIR / "models.py": """from django.db import models

# Aucun modèle nécessaire pour le moment.
""",
    CHAT_DIR / "admin.py": """# Aucun modèle à enregistrer pour le moment.
""",
    CHAT_DIR / "tests.py": """from django.test import TestCase


class ChatPageTests(TestCase):
    def test_home_page_is_available(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
""",
    CHAT_DIR / "migrations" / "__init__.py": "",
}

# Crée un fichier et ses dossiers parents uniquement s'il n'existe pas.
# Cette fonction évite d'écraser du code déjà écrit par le développeur.
def write_missing_file(path: Path, content: str) -> None:
    if path.exists():
        print(f"Déjà présent : {path.relative_to(PROJECT_ROOT)}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    print(f"Créé : {path.relative_to(PROJECT_ROOT)}")

# Vérifie que le projet Django principal existe,
# crée les fichiers manquants de l'application chat
# puis prépare les dossiers de templates et de fichiers statiques.
def main() -> None:
    
    # Interrompt le script lorsque la base du projet Django
    # n'a pas encore été créée.
    manage_py = PROJECT_ROOT / "apps" / "front" / "manage.py"
    settings_py = PROJECT_ROOT / "apps" / "front" / "config" / "settings.py"

    if not manage_py.exists() or not settings_py.exists():
        raise SystemExit(
            "Le projet Django principal est absent. "
            "Les fichiers manage.py et config/settings.py sont requis."
        )

    # Parcourt tous les fichiers attendus et crée uniquement ceux qui manquent.
    for path, content in FILES.items():
        write_missing_file(path, content)

    # Prépare les emplacements destinés aux templates HTML
    # et aux ressources statiques de l'application.
    (CHAT_DIR / "templates" / "chat").mkdir(
        parents=True,
        exist_ok=True,
    )
    (CHAT_DIR / "static" / "chat").mkdir(
        parents=True,
        exist_ok=True,
    )

    print("Structure du front Django prête.")

# Lance l'initialisation uniquement lorsque le script
# est exécuté directement.
if __name__ == "__main__":
    main()
