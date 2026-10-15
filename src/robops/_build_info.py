"""Informations injectées au moment de compiler l'exécutable.

Le workflow de release (.github/workflows/release.yml) réécrit ce fichier avant
d'appeler PyInstaller. En installation pip, les valeurs restent vides et la
mise à jour automatique est désactivée (on met à jour avec pip).
"""

# Dépôt GitHub qui publie les releases, ex. "mon-compte/robops".
REPOSITORY = ""
