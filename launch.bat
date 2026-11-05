@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title SILLAGE - Grille des ecarts

REM ============================================================
REM   SILLAGE - LANCEUR (interface graphique)
REM   Specifications, Incompletudes, Livrables :
REM   Lecture, Analyse et Grille des Ecarts
REM ============================================================
REM
REM  CLES API (passerelle d'IA, memes cles que RIVAGE) : de preference
REM  dans le fichier dedie RIVAGE_keys.env a cote de SILLAGE (modele :
REM  RIVAGE_keys.env.example). Vous pouvez aussi les definir ici :
REM set "RIVAGE_EMBED_KEY=cle-snowflake"        (recherche semantique)
REM set "RIVAGE_RERANK_KEY=cle-bge-reranker"    (scores d'appui)
REM set "RIVAGE_SYNTH_KEY=cle-mistral-small"    (OCR de secours a l'indexation)
REM
REM  Options du moteur (RIVAGE.env a cote de SILLAGE, modele : RIVAGE.env.example).
REM  Dossier de donnees de SILLAGE (dictionnaire des statuts, profils, index) :
REM set "SILLAGE_HOME=%LOCALAPPDATA%\sillage"
REM
REM  POSTES ANCIENS : au premier lancement, le moteur sonde le poste et
REM  memorise la configuration la plus rapide qui s'execute sans mourir.
REM  Rien a regler. Pour le support seulement :
REM set "RIVAGE_MODE_SUR=1"           (voie sure forcee, sans sonde)
REM set "RIVAGE_SIMD_LEVEL=AVX2"      (plafond : NONE, AVX2, AVX512)
REM ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERREUR] Environnement non installe.
    echo Lancez d'abord install.bat
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" sillage_app.py

if errorlevel 1 (
    echo.
    echo [ERREUR] L'application s'est arretee avec une erreur.
    pause
)
