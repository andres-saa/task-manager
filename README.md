# SalchiManager Pro Time

Sistema de administración de proyectos con gestión de tareas, timers, logs y adjuntos.

## Características

- ✅ Gestión de proyectos con jerarquía Áreas → Grupos → Proyectos
- ✅ Tareas recursivas (con subtareas ilimitadas)
- ✅ Timers en tiempo real para tareas
- ✅ Sistema de logs/bitácora
- ✅ Adjuntos de archivos
- ✅ Cálculo automático de progreso
- ✅ Filtrado y ordenamiento de proyectos
- ✅ Almacenamiento local en JSON (sin base de datos)

## Stack Tecnológico

- **Backend**: FastAPI
- **Templates**: Jinja2
- **CSS**: Tailwind CSS (CDN)
- **Almacenamiento**: JSON local (índice + archivos individuales)

## Instalación

1. Instalar dependencias:
```bash
pip install -r requirements.txt
```

2. Ejecutar la aplicación:

**Windows:**
```bash
start.bat
```

**Linux/Mac:**
```bash
chmod +x start.sh
./start.sh
```

**O manualmente:**
```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

3. Abrir en el navegador:
```
http://localhost:8000
```

## Solución de Problemas

### La aplicación se queda cargando o no arranca

Si la aplicación se queda congelada al iniciar, probablemente hay un archivo de lock bloqueado. Solución:

1. **Windows:**
   ```bash
   del data\index.json.lock
   ```

2. **Linux/Mac:**
   ```bash
   rm -f data/index.json.lock
   ```

3. Luego reinicia la aplicación.

El script `start.bat` o `start.sh` limpia automáticamente los lock files antiguos antes de iniciar.

## Estructura de Archivos

```
class-manager/
├── main.py              # Aplicación FastAPI principal
├── models.py            # Modelos Pydantic
├── storage.py           # Sistema de almacenamiento JSON
├── project_router.py    # Router con endpoints
├── templates/           # Templates Jinja2
│   ├── base.html
│   ├── dashboard.html
│   ├── project_detail.html
│   ├── task_item.html
│   └── modals.html
├── data/                # Datos (se crea automáticamente)
│   ├── index.json       # Índice global
│   └── projects/        # Archivos individuales por proyecto
└── attachments/         # Archivos adjuntos (se crea automáticamente)
```

## Uso

### Crear un Proyecto

1. En el dashboard, click en "Nuevo Proyecto"
2. Completa el formulario (nombre, descripción, área, grupo, fecha de entrega)
3. El proyecto se crea automáticamente

### Gestionar Tareas

1. Abre un proyecto desde el dashboard
2. Click en "Nueva Tarea" para añadir tareas raíz
3. Usa "Subtarea" para crear tareas anidadas
4. Controla el timer con "Iniciar" / "Pausar"
5. Marca como completada cuando termines

### Funcionalidades de Tareas

- **Iniciar**: Comienza a contar el tiempo
- **Pausar**: Detiene el timer y acumula el tiempo transcurrido
- **Completar**: Marca la tarea y todas sus subtareas como completadas
- **Subtarea**: Crea una tarea hija dentro de otra tarea
- **Eliminar**: Borra la tarea y todas sus subtareas

### Logs y Adjuntos

- **Logs**: Añade comentarios a la bitácora del proyecto
- **Adjuntos**: Sube archivos relacionados al proyecto (máx. 25MB)

### Áreas y Grupos

- Crea áreas y grupos para organizar tus proyectos
- Los proyectos se pueden mover entre áreas/grupos
- Al eliminar un área/grupo, los proyectos se reasignan automáticamente

## API Endpoints

### Proyectos
- `GET /` - Dashboard
- `GET /projects/{id}` - Detalle de proyecto
- `POST /api/projects/create` - Crear proyecto
- `POST /api/projects/{id}/update` - Actualizar proyecto
- `DELETE /api/projects/{id}` - Eliminar proyecto
- `POST /api/projects/{id}/postpone` - Posponer fecha

### Tareas
- `POST /api/projects/{id}/tasks` - Crear tarea raíz
- `POST /api/projects/{id}/tasks/{parent_id}/child` - Crear subtarea
- `PATCH /api/tasks/{id}/status` - Cambiar estado (running/paused/completed)
- `DELETE /api/tasks/{id}` - Eliminar tarea

### Áreas y Grupos
- `POST /api/areas/create` - Crear área
- `POST /api/groups/create` - Crear grupo
- `DELETE /api/areas/{id}` - Eliminar área
- `DELETE /api/groups/{id}` - Eliminar grupo

### Adjuntos y Logs
- `POST /api/projects/{id}/attachments` - Subir adjunto
- `POST /api/projects/{id}/logs` - Añadir log
- `GET /attachments/{filename}` - Descargar adjunto

## Notas

- Todos los datos se almacenan localmente en archivos JSON
- El índice global (`data/index.json`) contiene la estructura de áreas/grupos
- Cada proyecto tiene su propio archivo en `data/projects/{id}.json`
- Los timers se actualizan en tiempo real en el frontend
- El progreso se calcula automáticamente (tareas completadas / total)

## Desarrollo

Para desarrollo, se recomienda usar:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Esto permite acceso desde otros dispositivos en la red local.
