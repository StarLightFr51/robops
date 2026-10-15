"""Affichage console + intégration GitHub Actions.

Dans un runner (GITHUB_ACTIONS=true), les messages utilisent les commandes de
workflow (::group::, ::error::, ::notice::) et le résultat du déploiement est
écrit dans $GITHUB_OUTPUT et $GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager


def ensure_utf8_output() -> None:
    """Évite les plantages d'encodage sous Windows (✔, accents…) quand la sortie
    est redirigée vers un fichier ou un pipe en cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _escape(msg: str) -> str:
    # Encodage requis par les commandes de workflow GitHub.
    return msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def info(msg: str) -> None:
    print(msg, flush=True)


def success(msg: str) -> None:
    print(f"✔ {msg}", flush=True)


def notice(msg: str) -> None:
    if in_github_actions():
        print(f"::notice::{_escape(msg)}", flush=True)
    else:
        print(f"ℹ {msg}", flush=True)


def warn(msg: str) -> None:
    if in_github_actions():
        print(f"::warning::{_escape(msg)}", flush=True)
    else:
        print(f"⚠ {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    if in_github_actions():
        print(f"::error::{_escape(msg)}", flush=True)
    else:
        print(f"✖ {msg}", file=sys.stderr, flush=True)


@contextmanager
def group(title: str):
    if in_github_actions():
        print(f"::group::{title}", flush=True)
        try:
            yield
        finally:
            print("::endgroup::", flush=True)
    else:
        print(f"\n── {title}", flush=True)
        yield


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    value = str(value)
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            delim = f"EOF_{uuid.uuid4().hex}"
            fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")
        else:
            fh.write(f"{name}={value}\n")


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n")
