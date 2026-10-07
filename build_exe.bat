@echo off
REM Builds dist\GPOTrickBot.exe : one file, runs on any Windows PC without Python,
REM with your current setup (template, areas, routes, settings) baked in.
REM   build_exe.bat          -> includes everything, Discord webhook too (for your own PCs / alts)
REM   build_exe.bat public   -> same but WITHOUT your webhook (to give to someone else)
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

echo Installing build tools and dependencies...
%PY% -m pip install --upgrade pyinstaller mss opencv-python numpy pydirectinput keyboard rapidocr-onnxruntime
if errorlevel 1 goto fail

%PY% prepare_seed.py %1
if errorlevel 1 goto fail

echo Building...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name GPOTrickBot --icon icon.ico --add-data "icon.ico;." --add-data "fruit_icon.png;." --add-data "bucket_candycorn.png;." --add-data "bucket_basket.png;." --add-data "bucket_bag.png;." --add-data "chest_icon.png;." --add-data "knock_default.png;." --add-data "seed;seed" ^
  --collect-all rapidocr_onnxruntime --collect-all onnxruntime ^
  --hidden-import pydirectinput --hidden-import keyboard ^
  gpo_bot_app.py
if errorlevel 1 goto fail

echo.
echo Done: dist\GPOTrickBot.exe
echo It carries your setup inside. On first launch it unpacks it to %%APPDATA%%\GPOTrickBot and saves there.
pause
exit /b 0

:fail
echo.
echo Build failed - copy the error above.
pause
exit /b 1
