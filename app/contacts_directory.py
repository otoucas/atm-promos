"""Vérification défensive d'une demande d'ouverture de point de vente contre
le fichier maître du groupement (sigle / titulaire / emails connus). Ne bloque
jamais une création — les points de vente "dépannage" hors réseau y sont
légitimement absents — mais signale par email (voir send_suspicious_request_alert
dans store_requests.py) toute demande qui ne correspond pas à ce fichier, pour
qu'Olivier puisse vérifier après coup.
"""

import csv
import logging
from pathlib import Path

from . import config

logger = logging.getLogger("contacts_directory")

_EMAIL_COLUMNS = [
    "Mail Pharmacie (Clients / Laboratoires)",
    "Mail Titulaire 1",
    "Mail Titulaire 2",
    "Mail Titulaire 3",
    "Mail Commandes",
    "Mail Administratif",
]


def _local_part(email: str) -> str:
    return email.strip().lower().split("@")[0]


def load_directory() -> dict:
    """Retourne {sigle: {"name": str, "email_local_parts": set[str]}}.
    Dict vide si le fichier n'est pas configuré/présent — ne doit jamais
    faire échouer la création d'un point de vente. Le fichier est en
    ISO-8859-1 (export Excel), pas UTF-8."""
    path = config.CONTACTS_DIRECTORY_CSV_PATH
    if not path or not Path(path).is_file():
        return {}
    directory = {}
    try:
        with open(path, encoding="iso-8859-1", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                code = (row.get("Sigle") or "").strip().upper()
                if not code:
                    continue
                emails = {
                    _local_part(row[col])
                    for col in _EMAIL_COLUMNS
                    if row.get(col) and "@" in row[col]
                }
                directory[code] = {"name": (row.get("Nom") or "").strip(), "email_local_parts": emails}
    except Exception:
        logger.exception("Échec de lecture du fichier de contacts groupement (%s)", path)
        return {}
    return directory


def check_request(name: str, code: str, contact_email: str) -> list[str]:
    """Renvoie la liste des points étonnants pour cette demande (vide si RAS
    ou si le fichier est indisponible)."""
    directory = load_directory()
    if not directory:
        return []

    entry = directory.get(code.strip().upper())
    if not entry:
        return [f"Le sigle « {code} » n'apparaît pas dans le fichier groupement (nouveau point de vente hors réseau ?)."]

    reasons = []
    known_name = entry["name"]
    submitted_lower = name.strip().lower()
    known_lower = known_name.strip().lower()
    if known_name and known_lower not in submitted_lower and submitted_lower not in known_lower:
        reasons.append(f"Le nom soumis « {name} » ne correspond pas au nom connu « {known_name} » pour le sigle {code}.")

    if entry["email_local_parts"] and _local_part(contact_email) not in entry["email_local_parts"]:
        reasons.append(
            f"L'email de contact soumis ({contact_email}) ne correspond à aucune adresse connue pour {code} "
            "dans le fichier groupement."
        )
    return reasons
