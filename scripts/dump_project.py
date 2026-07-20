from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

# Chemin absolu vers la racine du dépôt.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Emplacement utilisé par défaut pour enregistrer le dump local.
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / ".local"
    / "project_dump.txt"
)

# Fichiers de configuration situés à la racine qui doivent être inclus.
ROOT_FILES = {
    ".env.example",
    ".gitignore",
    ".python-version",
    "Makefile",
    "pyproject.toml",
}

# Dossiers du projet qui seront parcourus récursivement.
SCAN_DIRECTORIES = (
    PROJECT_ROOT / "apps",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "tests",
)

# Extensions de fichiers texte autorisées dans le dump.
ALLOWED_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Dossiers techniques, volumineux ou sensibles à ne jamais inclure.
EXCLUDED_DIRECTORIES = {
    ".git",
    ".idea",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "media",
    "node_modules",
    "staticfiles",
}

# Fichiers précis qui doivent toujours être exclus du dump.
EXCLUDED_FILENAMES = {
    ".env",
    "db.sqlite3",
    "project_dump.txt",
}

# Extensions associées aux bases, clés, certificats
# ou fichiers compilés qui doivent être exclues.
EXCLUDED_SUFFIXES = {
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}

# Fragments de noms indiquant qu'un fichier peut contenir des secrets.
SENSITIVE_NAME_PARTS = {
    "credential",
    "private_key",
    "secret",
}

# Taille maximale autorisée pour un fichier inclus dans le dump.
MAX_FILE_SIZE = 1_000_000

# Expressions régulières utilisées pour détecter et masquer
# certaines clés API, jetons GitHub et clés privées.
REDACTIONS = (
    (
        re.compile(
            r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
        ),
        "[REDACTED_OPENAI_KEY]",
    ),
    (
        re.compile(
            r"gh[opsu]_[A-Za-z0-9]{20,}"
        ),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        re.compile(
            r"github_pat_[A-Za-z0-9_]{20,}"
        ),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        re.compile(
            (
                r"-----BEGIN [^-]*PRIVATE KEY-----"
                r".*?"
                r"-----END [^-]*PRIVATE KEY-----"
            ),
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
)

# Configure les arguments utilisables depuis le terminal :
# chemin de sortie et inclusion facultative du fichier uv.lock.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Regroupe les fichiers utiles du projet "
            "dans un fichier texte."
        ),
    )

    parser.add_argument(
        "--output",
        default=str(
            DEFAULT_OUTPUT.relative_to(PROJECT_ROOT)
        ),
        help=(
            "Chemin du fichier de sortie, relatif "
            "à la racine du projet."
        ),
    )

    parser.add_argument(
        "--include-lock",
        action="store_true",
        help=(
            "Inclut uv.lock, normalement ignoré "
            "car il peut être volumineux."
        ),
    )

    return parser.parse_args()

# Détermine si un fichier peut être inclus dans le dump.
# La fonction rejette les liens symboliques, fichiers sensibles,
# fichiers trop volumineux, bases de données et formats non autorisés.
def is_safe_file(
    path: Path,
    output_path: Path,
) -> bool:
    # Refuse les liens symboliques et les chemins qui ne sont pas des fichiers.
    if path.is_symlink() or not path.is_file():
        return False

    try:
        # Empêche le script d'inclure son propre fichier de sortie.
        # Vérifie que le fichier appartient bien à la racine du projet.
        if path.resolve() == output_path.resolve():
            return False

        relative_path = path.relative_to(
            PROJECT_ROOT
        )

    except (OSError, ValueError):
        return False
    # Exclut les fichiers situés dans un dossier interdit.
    if any(
        part in EXCLUDED_DIRECTORIES
        for part in relative_path.parts
    ):
        return False

    lower_name = path.name.lower()

    
    if lower_name in EXCLUDED_FILENAMES:
        return False

    # Exclut les fichiers d'environnement autres que l'exemple public.
    if (
        lower_name.startswith(".env")
        and lower_name != ".env.example"
    ):
        return False

    # Exclut les bases de données, certificats, clés privées
    # et fichiers Python compilés selon leur extension.
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False

    # Exclut les fichiers dont le nom semble contenir des informations sensibles.
    if any(
        part in lower_name
        for part in SENSITIVE_NAME_PARTS
    ):
        return False

    # Refuse les fichiers dépassant la taille maximale autorisée.
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return False

    except OSError:
        return False

    # Accepte uniquement les extensions prévues ou les fichiers racine autorisés.
    return (
        path.suffix.lower() in ALLOWED_SUFFIXES
        or path.name in ROOT_FILES
    )

# Recherche tous les fichiers autorisés à la racine et dans les dossiers
# analysés, retire les doublons puis les trie par chemin.
def collect_files(
    output_path: Path,
    include_lock: bool,
) -> list[Path]:
    candidates: set[Path] = set()

    # Ajoute les fichiers de configuration autorisés situés à la racine.
    for filename in ROOT_FILES:
        candidate = PROJECT_ROOT / filename

        if (
            candidate.exists()
            and is_safe_file(
                candidate,
                output_path,
            )
        ):
            candidates.add(candidate)

    # Ajoute uv.lock uniquement lorsque l'option correspondante est demandée.
    if include_lock:
        lock_file = PROJECT_ROOT / "uv.lock"

        if lock_file.exists():
            candidates.add(lock_file)

    # Parcourt récursivement chaque dossier sélectionné.
    for directory in SCAN_DIRECTORIES:
        if not directory.exists():
            continue

        for candidate in directory.rglob("*"):
            if is_safe_file(
                candidate,
                output_path,
            ):
                candidates.add(candidate)

    # Trie les fichiers pour produire un dump stable et facile à comparer.
    return sorted(
        candidates,
        key=lambda path: (
            path.relative_to(PROJECT_ROOT).as_posix()
        ),
    )

# Remplace dans un texte les clés et jetons reconnus
# par des marqueurs indiquant qu'ils ont été masqués.
def redact(content: str) -> str:
    for pattern, replacement in REDACTIONS:
        content = pattern.sub(
            replacement,
            content,
        )

    return content

# Construit le contenu final du dump en ajoutant un en-tête,
# puis une section clairement séparée pour chaque fichier.
def build_dump(files: list[Path]) -> str:
    # Enregistre la date de génération avec le fuseau horaire local.
    generated_at = (
        datetime.now()
        .astimezone()
        .isoformat(timespec="seconds")
    )

    # Prépare l'en-tête général donnant le nombre de fichiers inclus.
    chunks = [
        "MYCODER PROJECT DUMP",
        f"Generated at: {generated_at}",
        "Project root: .",
        f"Files included: {len(files)}",
        (
            "Secrets, virtual environments, caches "
            "and databases are excluded."
        ),
        "",
    ]

    # Lit chaque fichier et ajoute son chemin puis son contenu nettoyé.
    for path in files:
        relative_path = (
            path.relative_to(PROJECT_ROOT)
            .as_posix()
        )

        # Continue la génération même lorsqu'un fichier particulier est illisible.
        try:
            content = path.read_text(
                encoding="utf-8",
                errors="replace",
            )

        except OSError as exc:
            content = (
                f"[UNREADABLE FILE: {exc}]"
            )

        chunks.extend(
            [
                "=" * 88,
                f"FILE: {relative_path}",
                "=" * 88,
                # Applique le masquage des secrets avant d'ajouter le contenu au dump.
                redact(content).rstrip(),
                "",
            ]
        )    
    return "\n".join(chunks).rstrip() + "\n"

# Coordonne l'ensemble de la génération :
# lecture des arguments, préparation du chemin, sélection des fichiers,
# création du dump puis affichage d'un résumé dans le terminal.
def main() -> None:
    args = parse_args()

    # Transforme le chemin relatif fourni en chemin absolu dans le projet.
    output_path = Path(args.output)

    if not output_path.is_absolute():
        output_path = (
            PROJECT_ROOT
            / output_path
        )

    # Crée le dossier de destination lorsqu'il n'existe pas encore.
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Collecte tous les fichiers pouvant être inclus sans risque.
    files = collect_files(
        output_path,
        include_lock=args.include_lock,
    )

    # Génère puis écrit le contenu final dans le fichier de sortie.
    output_path.write_text(
        build_dump(files),
        encoding="utf-8",
    )

    # Calcule la taille du fichier généré en kibioctets
    # afin de l'afficher dans le résumé du terminal.
    size_kib = (
        output_path.stat().st_size
        / 1024
    )

    print(f"Dump créé : {output_path}")
    print(f"Fichiers inclus : {len(files)}")
    print(f"Taille : {size_kib:.1f} Kio")

# Exécute la génération uniquement lorsque le script
# est lancé directement depuis le terminal.
if __name__ == "__main__":
    main()
