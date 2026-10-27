@echo off
rem MyZip exe 빌드: dist\MyZip.exe (창 프로그램), dist\myzip-cli.exe (명령줄)
chcp 65001 >nul
cd /d "%~dp0"
set VENV=%TEMP%\mzbuild\venv
set WORK=%TEMP%\mzbuild\work

if not exist "%VENV%\Scripts\python.exe" (
    python -m venv "%VENV%" || exit /b 1
    "%VENV%\Scripts\python.exe" -m pip install -q pyinstaller || exit /b 1
)
if not exist myzip.ico python make_icon.py

"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name MyZip --icon "%CD%\myzip.ico" --workpath "%WORK%" --specpath "%WORK%" --distpath dist MyZip.pyw --log-level WARN || exit /b 1
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console --name myzip-cli --icon "%CD%\myzip.ico" --workpath "%WORK%" --specpath "%WORK%" --distpath dist myzip_cli.py --log-level WARN || exit /b 1
echo.
echo 빌드 완료: dist\MyZip.exe, dist\myzip-cli.exe
