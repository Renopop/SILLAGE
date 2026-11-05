# -*- mode: python ; coding: utf-8 -*-
# SILLAGE.spec — gèle SILLAGE en un exécutable autonome, moteur de RIVAGE
# embarqué compris (aucun Python ni RIVAGE requis sur le poste).
#
#   .venv\Scripts\pyinstaller --clean --noconfirm SILLAGE.spec
#   (ou simplement : build_exe.bat)
#
# Mêmes choix que RIVAGE.spec : ONEDIR (dist\SILLAGE\SILLAGE.exe +
# _internal\), fenêtré (sillage_app._fix_streams), collect_all sur les
# paquets à binaires/données, upx=False (faux positifs antivirus), et le
# hook d'exécution de la sonde de plateforme AVANT le premier import de faiss.

import os
from PyInstaller.utils.hooks import collect_all

MOTEUR = os.path.join("sillage", "moteur")

datas, binaries, hiddenimports = [], [], []
for pkg in ("faiss", "pymupdf", "certifi", "docx", "pptx"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Le moteur embarqué : modules plats importés par leur nom (sillage/moteur
# est sur le chemin d'import), plus la version de RIVAGE copiée.
hiddenimports += ["fitz", "pymupdf", "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
                  "plateforme", "diagnostic", "retrieval", "indexer", "index_cache", "embeddings", "synthesis",
                  "extraction", "chunking", "dedup", "pdf_processing", "docx_processing", "pptx_processing",
                  "excel_processing", "llm_ocr", "vector_index", "lexical_index", "registry", "store",
                  "content_store", "models_utils", "config", "version",
                  "sillage.gui", "sillage.cli", "sillage.appui", "sillage.affaiblissements", "sillage.rivage"]
datas += [(os.path.join(MOTEUR, "VERSION_RIVAGE.txt"), MOTEUR)]

a = Analysis(
    ["sillage_app.py"],
    pathex=[".", MOTEUR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[os.path.join(MOTEUR, "hooks", "rth_plateforme.py")],
    excludes=["pytest", "IPython", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SILLAGE",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    # icon="sillage.ico",   # décommenter si vous fournissez une icône .ico
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SILLAGE",
)
