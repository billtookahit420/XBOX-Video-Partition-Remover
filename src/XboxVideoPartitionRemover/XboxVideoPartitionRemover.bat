@echo off
rem XBOX Video Partition Remover - Windows launcher.
rem Needs Python 3 (with Tkinter, included in the python.org installer).
cd /d "%~dp0"
py -3 app\main.py %*
if errorlevel 1 python app\main.py %*
