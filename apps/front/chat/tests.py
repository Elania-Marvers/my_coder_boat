from django.test import TestCase

# Regroupe les tests automatiques vérifiant le fonctionnement
# minimal de la page de conversation Django.
class ChatPageTests(TestCase):

    # Vérifie qu'une requête GET vers la page d'accueil
    # retourne une réponse HTTP 200, indiquant que la page est accessible.
    def test_home_page_is_available(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
