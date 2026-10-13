import json 
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "robops.config.json"
OUTPUT_FILE = ROOT / ".github" / "workflows" / "deploy.yml"

def generate_workflow(config: dict) -> str:
    name = json.dumps(config["workflow_name"])
    branch = json.dumps(config["branch"])
    build_path = json.dumps(config["build_path"])
    environment = json.dumps(config["env"])
    publish_branch = config["branch"]

    return f"""name: {name}

on:
  pull_request:
    branches:
      - {branch}

  push:
    branches:
      - {branch}

  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: roblox-production
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v5

      - name: Install Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Prepare Rokit
        run: mkdir -p .rokit-bootstrap

      - name: Install Rokit
        uses: paradoxum-games/setup-rokit@v3
        with:
          path: .
          cache: true

      - name: Install Rojo
        run: rokit install --no-trust-check

      - name: Verify Rojo
        run: rojo --version

      - name: Build Roblox place
        run: |
          mkdir -p build
          rojo build default.project.json -o "build/game.rbxlx"
          test -s "build/game.rbxlx"

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: roblox-place
          path: "build/game.rbxlx"
          retention-days: 14

  publish:
    needs: build
    runs-on: ubuntu-latest
    environment: {environment}

    if: >-
      github.ref == 'refs/heads/{publish_branch}' &&
      (
        github.event_name == 'push' ||
        github.event_name == 'workflow_dispatch'
      )

    steps:
      - name: Checkout
        uses: actions/checkout@v5

      - name: Download built Roblox place
        uses: actions/download-artifact@v4
        with:
          name: roblox-place
          path: build

      - name: Publish to Roblox
        env:
          API_KEY: ${{{{ secrets.ROBLOX_API_KEY }}}}
          UNIVERSE_ID: ${{{{ secrets.ROBLOX_UNIVERSE_ID }}}}
          PLACE_ID: ${{{{ secrets.ROBLOX_PLACE_ID }}}}
        run: |
          set -euo pipefail

          response="$(mktemp)"
          trap 'rm -f "$response"' EXIT

          status="$(curl --silent --show-error \\
            --output "$response" \\
            --write-out '%{{http_code}}' \\
            --request POST \\
            "https://apis.roblox.com/universes/v1/$UNIVERSE_ID/places/$PLACE_ID/versions?versionType=Published" \\
            --header "x-api-key: $API_KEY" \\
            --header "Content-Type: application/xml" \\
            --data-binary "@{build_path}"

          if [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
            echo "Publication failed: HTTP $status"
            cat "$response"
            exit 1
          fi

          echo "Publication successful"
          cat "$response"
"""

def main():
    with CONFIG_FILE.open("r", encoding="utf-8") as file: 
        config = json.load(file)

    required = {
        "workflow_name",
        "branch",
        "build_path",
        "env",
    }

    missing = required - config.keys()
    if missing:
        raise ValueError(f"Missing configuration keys: {sorted(missing)}")

    workflow = generate_workflow(config)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(workflow, encoding="utf-8")

    print(f"Workflow generated: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
