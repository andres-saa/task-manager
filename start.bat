@echo off
echo Iniciando SalchiManager Pro Time...
echo.

REM Limpiar lock files antiguos si existen
if exist "data\index.json.lock" (
    echo Limpiando lock file antiguo...
    del /F /Q "data\index.json.lock" 2>nul
)

REM Iniciar servidor
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000

pause
