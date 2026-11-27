@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
title Frame Cutter
rem Lanceur Windows de Frame Cutter : vérifie Python 3.11 ou plus récent et Tkinter, puis ouvre la fenêtre.
rem Sécurité : aucun programme du dossier courant n'est lancé à la place du vrai Python.
set "NoDefaultCurrentDirectoryInExePath=1"
set "FC_DIR=%~dp0"
set "FC_PYTHON="

rem 1. Trouver Python 3.11 ou plus récent : d'abord le lanceur « py » installé avec Python, sinon « python ».
rem    Python donne lui-même son chemin complet, qui est ensuite utilisé tel quel.
for /f "usebackq delims=" %%P in (`py -3 -c "import sys; sys.version_info >= (3, 11) or sys.exit(1); print(sys.executable)" 2^>nul`) do if not defined FC_PYTHON set "FC_PYTHON=%%P"
if not defined FC_PYTHON for /f "usebackq delims=" %%P in (`python -c "import sys; sys.version_info >= (3, 11) or sys.exit(1); print(sys.executable)" 2^>nul`) do if not defined FC_PYTHON set "FC_PYTHON=%%P"
if defined FC_PYTHON if not exist "%FC_PYTHON%" set "FC_PYTHON="

if not defined FC_PYTHON (
    echo.
    echo Python 3.11 ou plus récent est introuvable sur cet ordinateur.
    echo Action : installez Python depuis https://www.python.org/downloads/windows/ en gardant les options proposées, puis relancez ce fichier.
    echo.
    pause
    exit /b 1
)

rem 2. Vérifier que Python contient Tkinter, le module qui dessine la fenêtre.
"%FC_PYTHON%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Le module d'interface Tkinter manque dans votre installation de Python.
    echo Action : relancez l'installation de Python, choisissez « Modify », cochez « tcl/tk and IDLE », puis relancez ce fichier.
    echo.
    pause
    exit /b 1
)

rem 3. Ouvrir Frame Cutter. Cette fenêtre reste ouverte en cas de problème pour que le message puisse être lu.
echo Frame Cutter s'ouvre. Cette fenêtre se fermera avec l'application.
"%FC_PYTHON%" "%FC_DIR%app.py"
if errorlevel 1 (
    echo.
    echo Frame Cutter s'est arrêté à cause d'un problème. Le message ci-dessus en donne la cause.
    echo Action : suivez l'action proposée dans ce message, puis relancez ce fichier.
    echo.
    pause
    exit /b 1
)
endlocal
exit /b 0
