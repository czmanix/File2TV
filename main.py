"""File2TV – přehrání videa z počítače na TV v síti (DLNA), náhrada „Vysílat do zařízení“."""

import os
import sys


def unblock_own_files() -> None:
    """Zip stažený z internetu má na každém souboru značku „z internetu“ (Zone.Identifier)
    a .NET (pythonnet, okno pywebview) pak odmítne načíst Python.Runtime.dll. Spuštění
    aplikace už uživatel potvrdil, značku z jejích souborů proto smažeme – stejně jako
    Vlastnosti → Odblokovat. Musí proběhnout před importem pywebview."""
    if not getattr(sys, "frozen", False):
        return
    for folder, _, files in os.walk(os.path.dirname(sys.executable)):
        for name in files:
            try:
                os.remove(os.path.join(folder, name) + ":Zone.Identifier")
            except OSError:
                pass  # značku nemá, nebo složka není zapisovatelná


unblock_own_files()

from tvcast.app import main  # noqa: E402

if __name__ == "__main__":
    main()
