from django.apps import AppConfig

# Déclare l'application Django « chat » et définit les informations
# utilisées par Django pour l'enregistrer dans le projet.
class ChatConfig(AppConfig):
    # Définit le type de clé primaire créé par défaut pour les futurs modèles.
    default_auto_field = "django.db.models.BigAutoField"
    
    # Indique le nom du module Python contenant l'application.
    name = "chat"

    # Définit le nom lisible de l'application dans l'administration Django.
    verbose_name = "MyCoder Chat"
