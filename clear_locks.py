"""
Script para limpiar lock files bloqueados.
Ejecutar si la aplicación se queda congelada.
"""
import os
from pathlib import Path

LOCK_FILE = Path("data/index.json.lock")

if LOCK_FILE.exists():
    try:
        LOCK_FILE.unlink()
        print(f"[OK] Lock file eliminado: {LOCK_FILE}")
    except Exception as e:
        print(f"[ERROR] No se pudo eliminar lock file: {e}")
        print("Intenta cerrar todas las instancias de la aplicación y ejecutar este script nuevamente.")
else:
    print("[OK] No hay lock file bloqueado.")
