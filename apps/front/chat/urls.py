from django.urls import path

from . import views

# Définit l'espace de noms utilisé pour référencer
# les routes de l'application dans les templates Django.
app_name = "chat"

# Associe les URL de l'application aux vues correspondantes :
# la racine affiche le chat et /clear/ efface la conversation.
urlpatterns = [
    path("", views.index, name="index"),
    path("clear/", views.clear_chat, name="clear"),
]
