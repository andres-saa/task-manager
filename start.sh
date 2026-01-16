#!/bin/bash
echo "Iniciando SalchiManager Pro Time..."
echo ""

# Limpiar lock files antiguos si existen
if [ -f "data/index.json.lock" ]; then
    echo "Limpiando lock file antiguo..."
    rm -f "data/index.json.lock"
fi

# Iniciar servidor
uvicorn main:app --reload --host 127.0.0.1 --port 8000
