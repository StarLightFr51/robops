"""Informations injectées au moment de compiler l'exécutable.

Le workflow de release (.github/workflows/release.yml) réécrit ce fichier avant
d'appeler PyInstaller. En installation pip, la mise à jour automatique reste
désactivée (on met à jour avec pip), même si REPOSITORY est renseigné.
"""

# Dépôt GitHub qui publie les releases.
REPOSITORY = "StarLightFr51/robops"
