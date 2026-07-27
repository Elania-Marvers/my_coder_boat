"""
FICHIER :
apps/front/chat/templatetags/chat_markdown.py

RÔLE GÉNÉRAL :
Expose un filtre de template Django transformant
les réponses Markdown de Qwen en HTML.

UTILISÉ PAR :
- apps/front/chat/templates/chat/index.html

SÉCURITÉ :
- le HTML brut contenu dans la réponse est désactivé ;
- seuls les éléments produits par le moteur Markdown
  sont marqués comme sûrs pour Django ;
- le contenu original reste stocké en texte brut
  dans la session Django.

PIPELINE :
CHAT_PAGE_DISPLAY
"""

from typing import Final

from django import template
from django.template.defaultfilters import stringfilter
from django.utils.safestring import (
    SafeString,
    mark_safe,
)
from markdown_it import MarkdownIt


# Enregistre les filtres déclarés dans ce module
# auprès du moteur de templates Django.
register = template.Library()


# RÔLE :
# Configure un moteur Markdown partagé par toutes
# les réponses affichées sur une même instance Django.
#
# CONFIGURATION :
# - js-default :
#   désactive le HTML brut et prend en charge
#   les tableaux ainsi que le texte barré ;
#
# - breaks=True :
#   transforme aussi les retours à la ligne simples
#   en balises <br> ;
#
# - linkify=False :
#   n'invente pas automatiquement des liens
#   à partir d'un simple texte ressemblant à une URL.
MARKDOWN_RENDERER: Final[MarkdownIt] = MarkdownIt(
    "js-default",
    {
        "breaks": True,
        "linkify": False,
        "typographer": False,
    },
)


# RÔLE :
# Convertit une chaîne Markdown en HTML.
#
# APPELÉ PAR :
# - index.html avec :
#   {{ message.content|render_markdown }}
#
# RETOURNE :
# - une SafeString contenant uniquement le HTML
#   généré par le moteur Markdown configuré ci-dessus.
@register.filter(name="render_markdown")
@stringfilter
def render_markdown(
    value: str,
) -> SafeString:
    rendered_html = MARKDOWN_RENDERER.render(
        value
    )

    # Le résultat peut être marqué comme sûr
    # car le preset js-default échappe le HTML brut
    # présent dans la réponse du modèle.
    return mark_safe(rendered_html)