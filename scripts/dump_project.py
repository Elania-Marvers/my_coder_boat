"""
FICHIER :
scripts/dump_project.py

RÔLE GÉNÉRAL :
Génère un fichier texte unique contenant les fichiers
utiles du projet MyCoder.

Ce dump facilite :

- la lecture globale de l'architecture ;
- la transmission du projet pour analyse ;
- la comparaison entre deux états du code ;
- le diagnostic sans envoyer les environnements virtuels ;
- la documentation du projet.

LE SCRIPT INCLUT :

- les fichiers de configuration publics de la racine ;
- les applications Django, FastAPI et worker ;
- les scripts du projet ;
- les tests ;
- la documentation située dans docs/.

LE SCRIPT EXCLUT :

- le fichier .env réel ;
- les bases de données ;
- les environnements virtuels ;
- les caches ;
- les fichiers compilés ;
- les clés privées ;
- les fichiers trop volumineux ;
- son propre fichier de sortie.

APPELÉ PAR :
- Makefile::dump
- commande directe :
  uv run python scripts/dump_project.py

PIPELINE :
- PROJECT_DUMP
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Final


# Racine absolue du dépôt MyCoder.
#
# Le script est situé dans :
#
# scripts/dump_project.py
#
# parents[1] correspond donc à la racine du projet.
PROJECT_ROOT: Final[Path] = (
    Path(__file__).resolve().parents[1]
)


# Emplacement utilisé lorsque l'utilisateur
# ne fournit pas l'option --output.
DEFAULT_OUTPUT: Final[Path] = (
    PROJECT_ROOT
    / ".local"
    / "project_dump.txt"
)


# Fichiers de la racine pouvant être ajoutés au dump.
#
# L'existence de chacun est vérifiée avant son inclusion.
# Le script reste donc compatible avec plusieurs noms
# de fichiers Docker Compose.
ROOT_FILES: Final[frozenset[str]] = frozenset(
    {
        ".env.example",
        ".gitignore",
        ".python-version",
        "Makefile",
        "README.md",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
        "pyproject.toml",
    }
)


# Dossiers parcourus récursivement.
#
# docs est désormais inclus afin que les fichiers
# de documentation apparaissent dans le dump.
SCAN_DIRECTORIES: Final[tuple[Path, ...]] = (
    PROJECT_ROOT / "apps",
    PROJECT_ROOT / "docs",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "tests",
)


# Extensions de fichiers texte autorisées.
ALLOWED_SUFFIXES: Final[frozenset[str]] = (
    frozenset(
        {
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
    )
)


# Dossiers techniques, volumineux ou sensibles
# qui ne doivent jamais être parcourus.
EXCLUDED_DIRECTORIES: Final[frozenset[str]] = (
    frozenset(
        {
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
    )
)


# Fichiers précis toujours exclus.
EXCLUDED_FILENAMES: Final[frozenset[str]] = (
    frozenset(
        {
            ".env",
            "db.sqlite3",
            "project_dump.txt",
        }
    )
)


# Extensions associées à des bases de données,
# certificats, clés privées ou fichiers compilés.
EXCLUDED_SUFFIXES: Final[frozenset[str]] = (
    frozenset(
        {
            ".db",
            ".key",
            ".p12",
            ".pem",
            ".pyc",
            ".sqlite",
            ".sqlite3",
        }
    )
)


# Fragments indiquant qu'un nom de fichier
# peut contenir des informations sensibles.
SENSITIVE_NAME_PARTS: Final[frozenset[str]] = (
    frozenset(
        {
            "credential",
            "private_key",
            "secret",
        }
    )
)


# Taille maximale d'un fichier normal inclus dans le dump.
MAX_FILE_SIZE: Final[int] = 1_000_000


# uv.lock peut être plus volumineux.
#
# Il n'est inclus que lorsque --include-lock
# est explicitement demandé.
MAX_LOCK_FILE_SIZE: Final[int] = 5_000_000


# Expressions utilisées pour masquer certains secrets
# qui auraient accidentellement été écrits dans un fichier.
#
# Chaque élément contient :
#
# 1. une expression régulière ;
# 2. le texte de remplacement.
REDACTIONS: Final[
    tuple[
        tuple[re.Pattern[str], str],
        ...,
    ]
] = (
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


# RÔLE :
# Déclare les options acceptées par le script.
#
# APPELÉE PAR :
# - main()
#
# OPTIONS :
# - --output :
#   emplacement du fichier produit ;
#
# - --include-lock :
#   ajoute uv.lock au dump.
#
# RETOURNE :
# - argparse.Namespace
#
# PIPELINE :
# - PROJECT_DUMP
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Regroupe les fichiers utiles du projet "
            "MyCoder dans un fichier texte."
        ),
    )

    parser.add_argument(
        "--output",
        default=str(
            DEFAULT_OUTPUT.relative_to(
                PROJECT_ROOT
            )
        ),
        help=(
            "Chemin de sortie absolu ou relatif "
            "à la racine du projet."
        ),
    )

    parser.add_argument(
        "--include-lock",
        action="store_true",
        help=(
            "Inclut uv.lock dans le dump. "
            "Ce fichier est ignoré par défaut "
            "car il peut être volumineux."
        ),
    )

    return parser.parse_args()


# RÔLE :
# Transforme le chemin fourni par l'utilisateur
# en chemin absolu.
#
# APPELÉE PAR :
# - main()
#
# COMPORTEMENT :
# - chemin absolu :
#   conservé tel quel ;
#
# - chemin relatif :
#   interprété depuis PROJECT_ROOT.
#
# RETOURNE :
# - Path absolu
#
# PIPELINE :
# - PROJECT_DUMP
def resolve_output_path(
    raw_output: str,
) -> Path:
    output_path = Path(raw_output).expanduser()

    if output_path.is_absolute():
        return output_path.resolve()

    return (
        PROJECT_ROOT
        / output_path
    ).resolve()


# RÔLE :
# Vérifie que le fichier appartient réellement
# à la racine du projet.
#
# APPELÉE PAR :
# - is_safe_file()
#
# RETOURNE :
# - chemin relatif au projet ;
# - None si le fichier se trouve ailleurs.
#
# SÉCURITÉ :
# Cela empêche le suivi accidentel d'un lien
# ou d'un chemin sortant du dépôt.
#
# PIPELINE :
# - PROJECT_DUMP
def get_project_relative_path(
    path: Path,
) -> Path | None:
    try:
        return path.resolve().relative_to(
            PROJECT_ROOT
        )

    except (OSError, ValueError):
        return None


# RÔLE :
# Vérifie qu'aucun dossier du chemin
# n'appartient à la liste des exclusions.
#
# APPELÉE PAR :
# - is_safe_file()
#
# RETOURNE :
# - True si un dossier interdit est trouvé ;
# - False sinon.
#
# PIPELINE :
# - PROJECT_DUMP
def is_inside_excluded_directory(
    relative_path: Path,
) -> bool:
    return any(
        part in EXCLUDED_DIRECTORIES
        for part in relative_path.parts
    )


# RÔLE :
# Vérifie que le nom du fichier ne semble pas
# annoncer un fichier contenant des secrets.
#
# APPELÉE PAR :
# - is_safe_file()
#
# RETOURNE :
# - True lorsque le nom paraît sensible ;
# - False sinon.
#
# PIPELINE :
# - PROJECT_DUMP
def has_sensitive_filename(
    path: Path,
) -> bool:
    lower_name = path.name.lower()

    return any(
        sensitive_part in lower_name
        for sensitive_part
        in SENSITIVE_NAME_PARTS
    )


# RÔLE :
# Vérifie qu'un fichier peut être ajouté au dump.
#
# APPELÉE PAR :
# - collect_root_files()
# - collect_scanned_files()
# - collect_lock_file()
#
# VÉRIFIE :
# - fichier réel et non lien symbolique ;
# - appartenance au projet ;
# - absence de dossier exclu ;
# - absence de nom sensible ;
# - extension autorisée ;
# - taille raisonnable ;
# - exclusion du fichier de sortie.
#
# RETOURNE :
# - True lorsque le fichier est sûr ;
# - False sinon.
#
# PIPELINE :
# - PROJECT_DUMP
def is_safe_file(
    path: Path,
    output_path: Path,
    *,
    allow_uv_lock: bool = False,
) -> bool:
    if path.is_symlink():
        return False

    if not path.is_file():
        return False

    try:
        if (
            path.resolve()
            == output_path.resolve()
        ):
            return False

    except OSError:
        return False

    relative_path = get_project_relative_path(
        path
    )

    if relative_path is None:
        return False

    if is_inside_excluded_directory(
        relative_path
    ):
        return False

    lower_name = path.name.lower()

    if lower_name in EXCLUDED_FILENAMES:
        return False

    # Autorise uniquement .env.example.
    #
    # Les fichiers comme :
    #
    # .env
    # .env.local
    # .env.production
    #
    # restent exclus.
    if (
        lower_name.startswith(".env")
        and lower_name != ".env.example"
    ):
        return False

    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False

    if has_sensitive_filename(path):
        return False

    try:
        file_size = path.stat().st_size

    except OSError:
        return False

    if allow_uv_lock and path.name == "uv.lock":
        return file_size <= MAX_LOCK_FILE_SIZE

    if file_size > MAX_FILE_SIZE:
        return False

    return (
        path.suffix.lower()
        in ALLOWED_SUFFIXES
        or path.name in ROOT_FILES
    )


# RÔLE :
# Collecte les fichiers publics
# situés directement à la racine.
#
# APPELÉE PAR :
# - collect_files()
#
# APPELLE :
# - is_safe_file()
#
# RETOURNE :
# - ensemble de Path
#
# PIPELINE :
# - PROJECT_DUMP
def collect_root_files(
    output_path: Path,
) -> set[Path]:
    collected_files: set[Path] = set()

    for filename in ROOT_FILES:
        candidate = PROJECT_ROOT / filename

        if not candidate.exists():
            continue

        if not is_safe_file(
            candidate,
            output_path,
        ):
            continue

        collected_files.add(candidate)

    return collected_files


# RÔLE :
# Parcourt apps, docs, scripts et tests.
#
# APPELÉE PAR :
# - collect_files()
#
# APPELLE :
# - Path.rglob()
# - is_safe_file()
#
# RETOURNE :
# - ensemble de Path
#
# PIPELINE :
# - PROJECT_DUMP
def collect_scanned_files(
    output_path: Path,
) -> set[Path]:
    collected_files: set[Path] = set()

    for directory in SCAN_DIRECTORIES:
        if not directory.exists():
            continue

        if not directory.is_dir():
            continue

        for candidate in directory.rglob("*"):
            if not is_safe_file(
                candidate,
                output_path,
            ):
                continue

            collected_files.add(candidate)

    return collected_files


# RÔLE :
# Ajoute uv.lock lorsque l'utilisateur
# a fourni l'option --include-lock.
#
# APPELÉE PAR :
# - collect_files()
#
# RETOURNE :
# - uv.lock lorsqu'il est présent et sûr ;
# - None sinon.
#
# PIPELINE :
# - PROJECT_DUMP
def collect_lock_file(
    output_path: Path,
    *,
    include_lock: bool,
) -> Path | None:
    if not include_lock:
        return None

    lock_file = PROJECT_ROOT / "uv.lock"

    if not is_safe_file(
        lock_file,
        output_path,
        allow_uv_lock=True,
    ):
        return None

    return lock_file


# RÔLE :
# Réunit tous les fichiers autorisés,
# retire les doublons et les trie.
#
# APPELÉE PAR :
# - main()
#
# APPELLE :
# - collect_root_files()
# - collect_scanned_files()
# - collect_lock_file()
#
# RETOURNE :
# - Liste stable de fichiers triés par chemin.
#
# PIPELINE :
# - PROJECT_DUMP
def collect_files(
    output_path: Path,
    *,
    include_lock: bool,
) -> list[Path]:
    candidates = collect_root_files(
        output_path
    )

    candidates.update(
        collect_scanned_files(
            output_path
        )
    )

    lock_file = collect_lock_file(
        output_path,
        include_lock=include_lock,
    )

    if lock_file is not None:
        candidates.add(lock_file)

    return sorted(
        candidates,
        key=lambda path: (
            path.relative_to(
                PROJECT_ROOT
            ).as_posix()
        ),
    )


# RÔLE :
# Masque les clés et jetons reconnus
# dans le contenu d'un fichier.
#
# APPELÉE PAR :
# - build_dump()
#
# RETOURNE :
# - Texte avec secrets remplacés.
#
# PIPELINE :
# - PROJECT_DUMP
def redact(
    content: str,
) -> str:
    redacted_content = content

    for pattern, replacement in REDACTIONS:
        redacted_content = pattern.sub(
            replacement,
            redacted_content,
        )

    return redacted_content


# RÔLE :
# Lit un fichier texte sans interrompre
# tout le dump si ce fichier est illisible.
#
# APPELÉE PAR :
# - build_dump()
#
# RETOURNE :
# - contenu du fichier ;
# - marqueur d'erreur en cas d'échec.
#
# PIPELINE :
# - PROJECT_DUMP
def read_text_file(
    path: Path,
) -> str:
    try:
        return path.read_text(
            encoding="utf-8",
            errors="replace",
        )

    except OSError as exc:
        return (
            "[UNREADABLE FILE: "
            f"{exc}]"
        )


# RÔLE :
# Construit le texte complet du dump.
#
# APPELÉE PAR :
# - main()
#
# APPELLE :
# - read_text_file()
# - redact()
#
# STRUCTURE :
#
# MYCODER PROJECT DUMP
# Generated at: ...
# Files included: ...
#
# FILE: chemin/du/fichier
# contenu
#
# RETOURNE :
# - chaîne complète prête à être écrite.
#
# PIPELINE :
# - PROJECT_DUMP
def build_dump(
    files: list[Path],
) -> str:
    generated_at = (
        datetime.now()
        .astimezone()
        .isoformat(timespec="seconds")
    )

    chunks = [
        "MYCODER PROJECT DUMP",
        f"Generated at: {generated_at}",
        "Project root: .",
        f"Files included: {len(files)}",
        (
            "Secrets, virtual environments, "
            "caches and databases are excluded."
        ),
        "",
    ]

    for path in files:
        relative_path = (
            path.relative_to(PROJECT_ROOT)
            .as_posix()
        )

        content = read_text_file(path)

        chunks.extend(
            [
                "=" * 88,
                f"FILE: {relative_path}",
                "=" * 88,
                redact(content).rstrip(),
                "",
            ]
        )

    return (
        "\n".join(chunks).rstrip()
        + "\n"
    )


# RÔLE :
# Coordonne toute la génération du dump.
#
# APPELÉE PAR :
# - bloc if __name__ == "__main__"
#
# APPELLE :
# - parse_args()
# - resolve_output_path()
# - collect_files()
# - build_dump()
# - Path.write_text()
#
# EFFETS :
# - crée le dossier de sortie ;
# - écrit le fichier ;
# - affiche le nombre de fichiers ;
# - affiche la taille finale.
#
# PIPELINE :
# - PROJECT_DUMP
def main() -> None:
    args = parse_args()

    output_path = resolve_output_path(
        args.output
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    files = collect_files(
        output_path,
        include_lock=args.include_lock,
    )

    dump_content = build_dump(files)

    output_path.write_text(
        dump_content,
        encoding="utf-8",
    )

    size_kib = (
        output_path.stat().st_size
        / 1024
    )

    print(
        f"Dump créé : {output_path}"
    )

    print(
        f"Fichiers inclus : {len(files)}"
    )

    print(
        f"Taille : {size_kib:.1f} Kio"
    )


# Lance le script uniquement lorsqu'il
# est exécuté directement.
if __name__ == "__main__":
    main()