from django.urls import include, path

# Redirige toutes les URL de la racine du site
# vers le fichier de routes de l'application chat.
urlpatterns = [
    path("", include("chat.urls")),
]
