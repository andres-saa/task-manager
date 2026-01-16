from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from project_router import router as project_router
from user_router import router as user_router
from auth import ensure_db_initialized
import os

# Asegúrate de crear las carpetas si no existen (evita errores 500)
os.makedirs("attachments", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("data/projects", exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el ciclo de vida de la aplicación."""
    # Startup: Inicializar base de datos al arrancar si no existe
    try:
        ensure_db_initialized()
        print("✓ Base de datos inicializada correctamente")
    except Exception as e:
        print(f"⚠ Warning: Error inicializando DB al arrancar: {e}")
    
    yield  # La aplicación corre aquí
    
    # Shutdown (opcional, para limpieza si es necesario)
    pass


app = FastAPI(title="SalchiManager Pro Time", lifespan=lifespan)

# IMPORTANTE: Montar archivos estáticos ANTES de incluir routers
# para evitar que los routers intercepten las rutas de archivos estáticos

# Servir archivos estáticos (avatares, etc.) - DEBE IR PRIMERO
os.makedirs("static/avatars", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Servir archivos adjuntos
app.mount("/attachments", StaticFiles(directory="attachments"), name="attachments")

# Incluir routers DESPUÉS de montar archivos estáticos
app.include_router(project_router)
app.include_router(user_router)