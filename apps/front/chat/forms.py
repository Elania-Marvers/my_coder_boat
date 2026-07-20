from django import forms

# Définit le formulaire utilisé par l'utilisateur pour envoyer
# un message au modèle local depuis l'interface Django.
class ChatForm(forms.Form):
    # Champ principal du formulaire contenant la question de l'utilisateur.
    # Il est obligatoire, limité à 8 000 caractères et affiché sous forme
    # de zone de texte dans l'interface.
    message = forms.CharField(
        label="",
        max_length=8_000,
        strip=True,
        widget=forms.Textarea(
            # Configure les attributs HTML du champ :
            # classe CSS, texte indicatif, hauteur initiale et mise au point automatique.
            attrs={
                "class": "composer__input",
                "placeholder": (
                    "Pose une question sur les bateaux, "
                    "l'histoire maritime…"
                ),
                "rows": 3,
                "autocomplete": "off",
                "autofocus": True,
            }
        ),
    )
