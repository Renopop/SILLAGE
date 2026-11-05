import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def sillage_home(tmp_path, monkeypatch):
    """Dossier de données isolé : rien n'est écrit dans le profil de l'utilisateur."""
    home = tmp_path / "sillage_home"
    monkeypatch.setenv("SILLAGE_HOME", str(home))
    # le moteur EMBARQUÉ, jamais un RIVAGE extérieur
    monkeypatch.delenv("SILLAGE_RIVAGE_DIR", raising=False)
    return home


def ecrire_xlsx(chemin, lignes, feuille="NCM", fusions=()):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = feuille
    for l in lignes:
        ws.append(l)
    for f in fusions:
        ws.merge_cells(f)
    wb.save(str(chemin))
    return chemin


def ecrire_docx(chemin, paragraphes=(), tables=()):
    import docx
    doc = docx.Document()
    for p in paragraphes:
        if isinstance(p, tuple):
            doc.add_paragraph(p[0], style=p[1])
        else:
            doc.add_paragraph(p)
    for t in tables:
        table = doc.add_table(rows=0, cols=len(t[0]))
        for l in t:
            cells = table.add_row().cells
            for c, v in zip(cells, l):
                c.text = str(v)
    doc.save(str(chemin))
    return chemin


SPEC = [
    ["ID", "Exigence", "Norme"],
    ["REQ-001", "Le calculateur doit peser au plus 2 kg.", "CS 25.1309"],
    ["REQ-002", "Le calculateur doit fonctionner de -40 °C à +70 °C.", ""],
    ["REQ-003", "Le calculateur doit être alimenté en 28 V DC.", "CS 25.1353"],
    ["REQ-004", "Le logiciel doit être développé au niveau DAL B.", "CS 25.1309"],
    ["REQ-005", "Le calculateur doit signaler toute panne interne.", "CS 25.1309"],
    ["REQ-006", "La documentation doit être fournie en anglais.", ""],
]

NCM_A = [
    ["Req ID", "Compliance", "Justification", "Action plan", "Waiver ref"],
    ["REQ-001", "C", "Masse mesurée 1,8 kg (rapport T-12).", "", ""],
    ["REQ-002", "PC", "Qualifié de -40 °C à +55 °C seulement.", "Essai complémentaire à +70 °C planifié", ""],
    ["REQ-003", "C", "Alimentation 28 V DC conforme DO-160 §16.", "", ""],
    ["REQ-004", "NC", "Logiciel développé au niveau DAL C.", "", "RFD-0007"],
    ["REQ-005", "Compliant (see note 2)", "BITE intégré.", "", ""],
    ["REQ-009", "C", "Identifiant inconnu du référentiel.", "", ""],
]
