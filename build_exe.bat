@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo   SILLAGE - Construction de l'executable autonome
echo   (dist\SILLAGE\SILLAGE.exe + _internal\ : aucun Python requis)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERREUR] Environnement non installe. Lancez d'abord install.bat
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installation de PyInstaller ...
    ".venv\Scripts\python.exe" -m pip install "pyinstaller>=6.0"
    if errorlevel 1 (
        echo [ERREUR] Echec de l'installation de PyInstaller.
        pause
        exit /b 1
    )
)

echo Construction ...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm SILLAGE.spec
if errorlevel 1 (
    echo [ERREUR] La construction a echoue.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Executable construit : dist\SILLAGE\SILLAGE.exe
echo   Copiez le dossier dist\SILLAGE\ tel quel sur les postes, avec
echo   RIVAGE_keys.env (et RIVAGE.env si besoin) a cote de l'exe.
echo ============================================================
echo.
pause
