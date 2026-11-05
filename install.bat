@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo   SILLAGE - Installation
echo ============================================================
echo.

REM --- 1. Verifier que Python est disponible -------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python est introuvable dans le PATH.
    echo.
    echo Installez Python 3.9 ou superieur depuis https://www.python.org/
    echo et cochez "Add python.exe to PATH" pendant l'installation.
    echo.
    pause
    exit /b 1
)

python --version
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 (
    echo [ERREUR] Python 3.9 ou superieur est requis et fonctionnel.
    echo.
    pause
    exit /b 1
)
echo.

REM --- 2. Creer l'environnement virtuel .venv ------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creation de l'environnement virtuel .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERREUR] Echec de la creation de l'environnement virtuel.
        pause
        exit /b 1
    )
) else (
    echo Environnement virtuel .venv deja present.
)
echo.

REM --- 3. Mettre a jour pip ------------------------------------
echo Mise a jour de pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
echo.

REM --- 4. Installer les dependances ---------------------------
echo Installation des dependances (requirements.txt) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    pause
    exit /b 1
)
echo.

REM --- 5. Controle de bon fonctionnement ----------------------
echo Verification des composants essentiels ...
".venv\Scripts\python.exe" -c "import numpy, httpx, faiss, fitz, pdfplumber, docx, pptx, openpyxl, xlrd, pandas, psutil, tkinter" 2>nul
if errorlevel 1 (
    echo [AVERTISSEMENT] Au moins un composant ne s'importe pas. Detail :
    for %%m in (numpy httpx faiss fitz pdfplumber docx pptx openpyxl xlrd pandas psutil tkinter) do (
        ".venv\Scripts\python.exe" -c "import %%m" 2>nul || echo   - %%m : ECHEC
    )
    echo.
    echo L'application peut fonctionner partiellement. Relancez install.bat
    echo apres avoir corrige la cause ^(reseau/proxy pip, antivirus...^).
) else (
    echo Composants essentiels : OK
)
echo.

echo ============================================================
echo   Installation terminee.
echo.
echo   CLES API ^(passerelle d'IA, memes cles que RIVAGE^) : copiez
echo   RIVAGE_keys.env.example en RIVAGE_keys.env a cote de SILLAGE
echo   et renseignez RIVAGE_EMBED_KEY et RIVAGE_RERANK_KEY
echo   ^(RIVAGE_SYNTH_KEY pour l'OCR de secours a l'indexation^).
echo   Sans cles : lecture des NCM, grille, diff et affaiblissements
echo   fonctionnent ; la verification des "C" reste "non verifie".
echo.
echo   - Interface graphique   : lancez launch.bat
echo   - Ligne de commande     : .venv\Scripts\python sillage_app.py --help
echo   - Executable autonome   : build_exe.bat ^(dist\SILLAGE\^)
echo ============================================================
echo.
pause
