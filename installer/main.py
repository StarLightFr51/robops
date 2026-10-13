# WARNING : SI C UN EXE ALORS C PAS DANS LE zipball_url MAIS PLUTOT DANS LE assets

import requests

REPO = "StarLightFr51/AutoHost"

# 1. Récupérer le dernier release
r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest")
r.raise_for_status()
release = r.json()

print(f"Version: {release['tag_name']}")

# 2. Télécharger le zipball (archive source du tag)
url = release["zipball_url"]
print(f"Téléchargement de {url} ...")

resp = requests.get(url, stream=True, allow_redirects=True)
resp.raise_for_status()

filename = f"{REPO.split('/')[1]}-{release['tag_name']}.zip"
with open(filename, "wb") as f:
    for chunk in resp.iter_content(1024 * 1024):
        f.write(chunk)

print(f"✅ Sauvegardé dans {filename}")