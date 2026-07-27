"""
FICHIER :
tests/unit/front/test_markdown.py

RÔLE GÉNÉRAL :
Teste le rendu Markdown utilisé pour afficher
les réponses de Qwen dans le front Django.

ÉLÉMENTS TESTÉS :
- texte en gras ;
- titres et listes ;
- blocs de code ;
- tableaux ;
- retours à la ligne ;
- neutralisation du HTML brut ;
- refus des liens JavaScript ;
- marquage sûr du HTML final.

PIPELINE :
- TEST
- CHAT_PAGE_DISPLAY
"""

from django.utils.safestring import SafeData

from chat.templatetags.chat_markdown import (
    render_markdown,
)


# Vérifie le rendu d'une emphase forte.
def test_render_markdown_formats_bold_text() -> None:
    html = str(
        render_markdown(
            "Texte **important**."
        )
    )

    assert (
        "<strong>important</strong>"
        in html
    )


# Vérifie les titres et listes numérotées.
def test_render_markdown_formats_heading_and_list() -> None:
    html = str(
        render_markdown(
            "## Étapes\n\n"
            "1. Première étape\n"
            "2. Deuxième étape"
        )
    )

    assert "<h2>Étapes</h2>" in html
    assert "<ol>" in html
    assert "<li>Première étape</li>" in html
    assert "<li>Deuxième étape</li>" in html


# Vérifie la génération d'un bloc de code
# avec le nom du langage dans sa classe CSS.
def test_render_markdown_formats_fenced_code() -> None:
    html = str(
        render_markdown(
            "```python\n"
            'print("bonjour")\n'
            "```"
        )
    )

    assert "<pre>" in html

    assert (
        '<code class="language-python">'
        in html
    )

    # Les guillemets du code sont échappés
    # dans le document HTML final.
    assert "print(&quot;bonjour&quot;)" in html


# Vérifie que le preset choisi
# prend en charge les tableaux.
def test_render_markdown_formats_table() -> None:
    html = str(
        render_markdown(
            "| Nom | Type |\n"
            "| --- | --- |\n"
            "| Qwen | Local |"
        )
    )

    assert "<table>" in html
    assert "<th>Nom</th>" in html
    assert "<td>Qwen</td>" in html


# Vérifie que les retours à la ligne simples
# sont visibles dans une réponse.
def test_render_markdown_preserves_line_breaks() -> None:
    html = str(
        render_markdown(
            "Première ligne\n"
            "Deuxième ligne"
        )
    )

    assert "<br>" in html


# Vérifie que du HTML produit ou recopié
# par le modèle n'est jamais exécuté.
def test_render_markdown_escapes_raw_html() -> None:
    html = str(
        render_markdown(
            '<script>alert("danger")</script>'
        )
    )

    assert "<script>" not in html

    assert "&lt;script&gt;" in html
    assert "&lt;/script&gt;" in html


# Vérifie qu'un lien utilisant JavaScript
# ne devient pas un lien HTML exécutable.
def test_render_markdown_rejects_javascript_link() -> None:
    html = str(
        render_markdown(
            "[Lien dangereux]"
            "(javascript:alert('danger'))"
        )
    )

    assert (
        'href="javascript:'
        not in html.lower()
    )


# Vérifie que Django ne rééchappe pas
# les balises générées par le parseur.
def test_render_markdown_returns_safe_string() -> None:
    html = render_markdown(
        "**Texte sûr**"
    )

    assert isinstance(
        html,
        SafeData,
    )