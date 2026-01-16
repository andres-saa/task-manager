"""
Router principal para gestión de proyectos.
"""
import os
import uuid
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from fastapi import APIRouter, UploadFile, File, Request, Form, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from storage import (
    load_index, save_index, load_project, save_project, delete_project,
    list_all_projects, normalize_project, normalize_task, normalize_attachment,
    find_task_by_id, remove_task_by_id, calculate_progress, total_sec_from_tasks, get_task_total_seconds,
    total_sec_from_tasks_with_running, has_running_tasks, sync_derived_completion, DEFAULT_AREA_ID, DEFAULT_GROUP_ID, _now_ts,
    load_teams, save_teams, get_team, create_team, update_team, delete_team, expand_team_members
)
from models import Project, Task, Area, Group, LogEntry, Attachment, Team
from auth import get_current_user, require_user, get_all_users, get_user_avatar_url, _is_admin, sync_users_if_stale

router = APIRouter(prefix="", tags=["Projects"])
templates = Jinja2Templates(directory="templates")

# Ruta para servir avatares (respaldo si el mount no funciona)
@router.get("/api/avatars/{filename}")
async def serve_avatar(filename: str):
    """Sirve las imágenes de avatares como respaldo."""
    avatar_path = Path("static/avatars") / filename
    if avatar_path.exists() and avatar_path.is_file():
        return FileResponse(avatar_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Avatar not found")

# Filtros personalizados de Jinja2
def format_duration_filter(seconds: int) -> str:
    """Filtro para formatear duración."""
    return format_duration(seconds)

def timestamp_to_date(ts: float) -> str:
    """Filtro para convertir timestamp a fecha legible."""
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M")

templates.env.filters["format_duration"] = format_duration_filter
templates.env.filters["timestamp_to_date"] = timestamp_to_date
templates.env.globals["now"] = time.time
templates.env.globals["get_task_total_seconds"] = get_task_total_seconds

# Directorios
ATTACHMENTS_DIR = Path("attachments")
ATTACHMENTS_DIR.mkdir(exist_ok=True)

MAX_ATTACH_MB = 25
MAX_ATTACH_BYTES = int(MAX_ATTACH_MB * 1024 * 1024)

ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "webp", "gif",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv", "zip", "rar", "mp3", "wav",
    "mp4", "mov", "py", "js", "html", "css", "json", "sql"
}


# ============================================================
# Helpers
# ============================================================

def format_duration(seconds: int) -> str:
    """Convierte segundos a formato '1h 30m 10s'."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    
    return " ".join(parts)


def days_to_deadline(deadline: Optional[float]) -> Optional[int]:
    """Calcula días restantes hasta deadline."""
    if not deadline:
        return None
    now = _now_ts()
    delta = deadline - now
    days = int(delta / 86400)
    return days


def get_urgency_class(deadline: Optional[float]) -> str:
    """Retorna clase CSS según urgencia."""
    days = days_to_deadline(deadline)
    if days is None:
        return "text-gray-500"
    if days < 0:
        return "text-red-600 font-bold"
    if days <= 3:
        return "text-red-500"
    if days <= 7:
        return "text-yellow-500"
    return "text-green-500"


def save_attachment(upload: UploadFile, actor_id: str) -> Attachment:
    """Guarda un archivo adjunto."""
    filename = upload.filename or ""
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: .{ext}")
    
    attach_id = uuid.uuid4().hex[:10]
    stored_name = f"{attach_id}.{ext}"
    path = ATTACHMENTS_DIR / stored_name
    
    total = 0
    with open(path, "wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ATTACH_BYTES:
                try:
                    out.close()
                    path.unlink()
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail=f"Archivo supera {MAX_ATTACH_MB}MB")
            out.write(chunk)
    
    return Attachment(
        id=attach_id,
        filename=stored_name,
        original_name=filename,
        url=f"/attachments/{stored_name}",
        size=total,
        content_type=upload.content_type or "",
        uploaded_at=_now_ts(),
        uploaded_by=actor_id
    )


def get_attachment_url(attachment: Dict[str, Any]) -> str:
    """Genera URL correcta para adjuntos (legacy o nuevo)."""
    if "url" in attachment:
        return attachment["url"]
    if "filename" in attachment:
        return f"/attachments/{attachment['filename']}"
    return ""


# ============================================================
# Health Check
# ============================================================

@router.get("/health")
async def health_check():
    """Endpoint de salud para verificar que la app funciona."""
    try:
        index = load_index()
        return JSONResponse({
            "status": "ok",
            "areas_count": len(index.areas),
            "groups_count": len(index.groups),
            "projects_count": len(index.projects)
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        }, status_code=500)


# ============================================================
# Páginas HTML
# ============================================================

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    q: Optional[str] = Query(None),
    area_id: Optional[str] = Query(None),
    group_id: Optional[str] = Query(None),
    sort: Optional[str] = Query("deadline")
):
    """Dashboard principal con grid de proyectos."""
    # Sincronizar usuarios si es necesario
    sync_users_if_stale(force=False)
    
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    
    try:
        index = load_index(use_cache=True)
        all_projects = list_all_projects()
        
        # Filtrar proyectos según permisos (usuarios solo ven sus proyectos asignados)
        if not _is_admin(user):
            user_projects = [p for p in all_projects if user["id"] in p.assigned_users]
            all_projects = user_projects
    except Exception as e:
        return HTMLResponse(f"<h1>Error cargando datos</h1><p>{str(e)}</p>", status_code=500)
    
    # Filtrar proyectos
    filtered = []
    for project in all_projects:
        # Búsqueda por texto
        if q and q.strip():
            q_lower = q.lower().strip()
            if (q_lower not in project.name.lower() and 
                q_lower not in (project.description or "").lower()):
                continue
        
        # Filtro por área (solo si se especifica y no está vacío)
        if area_id and area_id.strip():
            if project.area_id != area_id.strip():
                continue
        
        # Filtro por grupo (solo si se especifica y no está vacío)
        if group_id and group_id.strip():
            if project.group_id != group_id.strip():
                continue
        
        filtered.append(project)
    
    # Ordenar
    if sort == "deadline":
        filtered.sort(key=lambda p: p.deadline or float('inf'))
    elif sort == "progress":
        filtered.sort(key=lambda p: calculate_progress(p.tasks), reverse=True)
    elif sort == "name":
        filtered.sort(key=lambda p: p.name.lower())
    
    # Enriquecer con datos calculados
    now = _now_ts()
    enriched = []
    for project in filtered:
        progress = calculate_progress(project.tasks)
        total_sec = total_sec_from_tasks_with_running(project.tasks, now)
        days = days_to_deadline(project.deadline)
        urgency_class = get_urgency_class(project.deadline)
        has_running = has_running_tasks(project.tasks)
        
        enriched.append({
            "project": project,
            "progress": progress,
            "total_time": format_duration(total_sec),
            "total_time_sec": total_sec,
            "days_to_deadline": days,
            "urgency_class": urgency_class,
            "has_running_tasks": has_running,
            "tasks_json": [task.model_dump() for task in project.tasks]  # Para JavaScript
        })
    
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "projects": enriched,
        "index": index,
        "q": q or "",
        "area_id": area_id or "",
        "group_id": group_id or "",
        "sort": sort or "deadline",
        "user": user
    })


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    """Página de detalle de proyecto."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Verificar permisos
    if not _is_admin(user) and user["id"] not in project.assigned_users:
        raise HTTPException(status_code=403, detail="No tienes acceso a este proyecto")
    
    index = load_index()
    area = next((a for a in index.areas if a.id == project.area_id), None)
    group = next((g for g in index.groups if g.id == project.group_id), None)
    
    progress = calculate_progress(project.tasks)
    now = _now_ts()
    total_sec = total_sec_from_tasks_with_running(project.tasks, now)
    days = days_to_deadline(project.deadline)
    urgency_class = get_urgency_class(project.deadline)
    has_running = has_running_tasks(project.tasks)
    
    # Obtener usuarios para mostrar
    all_users = get_all_users()
    assigned_users_info = []
    for u in all_users:
        if u.get("id") in project.assigned_users:
            assigned_users_info.append({
                "id": u.get("id"),
                "username": u.get("username"),
                "display_name": u.get("display_name"),
                "role": u.get("role"),
                "site_name": u.get("site_name", ""),
                "dni": u.get("dni", ""),
                "gender": u.get("gender", ""),
                "avatar_url": get_user_avatar_url(u)
            })
    
    # Obtener equipos para agrupar usuarios y convertirlos a diccionarios
    from storage import load_teams
    all_teams_models = load_teams()
    all_teams = [team.model_dump() for team in all_teams_models]
    
    # Preparar usuarios con todos los campos necesarios
    all_users_with_extra = []
    for u in all_users:
        all_users_with_extra.append({
            "id": u.get("id"),
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "role": u.get("role"),
            "site_name": u.get("site_name", ""),
            "dni": u.get("dni", ""),
            "gender": u.get("gender", ""),
            "avatar_url": get_user_avatar_url(u)
        })
    
    # Convertir tareas a diccionarios para serialización JSON
    tasks_dict = [task.model_dump() for task in project.tasks]
    
    # Convertir attachments a diccionarios para serialización JSON
    attachments_dict = [att.model_dump() for att in (project.attachments or [])]
    
    # Verificar si el usuario es el creador del proyecto
    is_project_creator = project.created_by == user["id"]
    
    return templates.TemplateResponse("project_detail.html", {
        "request": request,
        "project": project,
        "area": area,
        "group": group,
        "progress": progress,
        "total_time": format_duration(total_sec),
        "total_time_sec": total_sec,
        "days_to_deadline": days,
        "urgency_class": urgency_class,
        "has_running_tasks": has_running,
        "index": index,
        "user": user,
        "all_users": all_users_with_extra,
        "assigned_users": assigned_users_info,
        "all_teams": all_teams,
        "project_tasks_json": tasks_dict,
        "project_attachments_json": attachments_dict,
        "is_project_creator": is_project_creator
    })


@router.get("/api/projects/{project_id}")
async def api_get_project(project_id: str, request: Request):
    """API para obtener proyecto (para JavaScript)."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Verificar permisos
    if not _is_admin(user) and user["id"] not in project.assigned_users:
        raise HTTPException(status_code=403, detail="No tienes acceso a este proyecto")
    
    return JSONResponse({
        "project": project.model_dump()
    })


# ============================================================
# API: Proyectos
# ============================================================

@router.post("/api/projects/create")
async def create_project(request: Request, data: dict = Body(...)):
    """Crea un nuevo proyecto."""
    user = require_user(request)
    
    name = (data.get("name") or "").strip()
    area_id = (data.get("area_id") or DEFAULT_AREA_ID).strip()
    group_id = (data.get("group_id") or DEFAULT_GROUP_ID).strip()
    
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")
    
    index = load_index()
    
    # Validar área y grupo
    if not any(a.id == area_id for a in index.areas):
        area_id = DEFAULT_AREA_ID
    if not any(g.id == group_id for g in index.groups):
        group_id = DEFAULT_GROUP_ID
    
    project_id = uuid.uuid4().hex[:12]
    assigned_users = data.get("assigned_users", [])
    if not isinstance(assigned_users, list):
        assigned_users = []
    
    project = Project(
        id=project_id,
        name=name,
        description=(data.get("description") or "").strip(),
        area_id=area_id,
        group_id=group_id,
        deadline=data.get("deadline"),
        created_at=_now_ts(),
        created_by=user["id"],
        assigned_users=assigned_users,
        tasks=[],
        logs=[],
        attachments=[]
    )
    
    try:
        save_project(project)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error guardando proyecto: {e}")
    
    # Actualizar índice
    index.projects[project_id] = {
        "area_id": area_id,
        "group_id": group_id
    }
    try:
        save_index(index)
    except Exception as e:
        # Si falla guardar el índice, el proyecto ya está guardado
        pass
    
    # Añadir log automático
    log = LogEntry(
        id=uuid.uuid4().hex[:10],
        text=f"Proyecto creado",
        created_at=_now_ts(),
        created_by="system"
    )
    project.logs.insert(0, log)
    save_project(project)
    
    return JSONResponse({"status": "ok", "project_id": project_id})


@router.post("/api/projects/{project_id}/update")
async def update_project(project_id: str, request: Request, data: dict = Body(...)):
    """Actualiza un proyecto."""
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    if "name" in data:
        project.name = (data.get("name") or "").strip()
    if "description" in data:
        project.description = (data.get("description") or "").strip()
    if "deadline" in data:
        project.deadline = data.get("deadline")
    if "area_id" in data or "group_id" in data:
        # Mover proyecto a otro área/grupo
        index = load_index()
        new_area_id = (data.get("area_id") or project.area_id).strip()
        new_group_id = (data.get("group_id") or project.group_id).strip()
        
        if new_area_id != project.area_id or new_group_id != project.group_id:
            project.area_id = new_area_id
            project.group_id = new_group_id
            index.projects[project_id] = {
                "area_id": new_area_id,
                "group_id": new_group_id
            }
            save_index(index)
    
    save_project(project)
    return JSONResponse({"status": "ok"})


@router.delete("/api/projects/{project_id}")
async def delete_project_endpoint(project_id: str, request: Request):
    """Elimina un proyecto. Solo el creador o administradores pueden eliminar."""
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto o administradores pueden eliminar
    if not _is_admin(user) and project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto o administradores pueden eliminar proyectos")
    
    # Eliminar del índice
    index = load_index()
    if project_id in index.projects:
        del index.projects[project_id]
        save_index(index)
    
    # Eliminar archivo
    delete_project(project_id)
    
    return JSONResponse({"status": "ok"})


@router.post("/api/projects/{project_id}/postpone")
async def postpone_project(project_id: str, request: Request, data: dict = Body(...)):
    """Posponer fecha de entrega."""
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    days = int(data.get("days", 7))
    if project.deadline:
        project.deadline += (days * 86400)
    else:
        project.deadline = _now_ts() + (days * 86400)
    
    # Añadir log
    log = LogEntry(
        id=uuid.uuid4().hex[:10],
        text=f"Fecha de entrega pospuesta {days} días",
        created_at=_now_ts(),
        created_by="system"
    )
    project.logs.insert(0, log)
    
    save_project(project)
    return JSONResponse({"status": "ok", "new_deadline": project.deadline})


# ============================================================
# API: Tareas
# ============================================================

@router.post("/api/projects/{project_id}/tasks")
async def add_root_task(project_id: str, request: Request):
    """Añade una tarea al nivel raíz. Solo el creador del proyecto puede agregar tareas."""
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto puede agregar tareas
    if project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto puede agregar tareas")
    
    # Intentar obtener datos como JSON primero
    try:
        data = await request.json()
    except:
        # Si no es JSON, intentar como form data
        form = await request.form()
        data = {
            "title": form.get("title", ""),
            "description": form.get("description", "")
        }
    
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="El título es requerido")
    
    task = Task(
        id=uuid.uuid4().hex[:10],
        title=title,
        description=(data.get("description") or "").strip(),
        status="pending",
        children=[],
        attachments=[]
    )
    
    project.tasks.append(task)
    save_project(project)
    
    # Crear log automático
    create_task_log(project, task, "task_create", user)
    save_project(project)
    
    return JSONResponse({"status": "ok", "task_id": task.id})


@router.post("/api/projects/{project_id}/tasks/{parent_id}/child")
async def add_child_task(project_id: str, parent_id: str, request: Request):
    """Añade una subtarea. Solo el creador del proyecto puede agregar subtareas."""
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto puede agregar subtareas
    if project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto puede agregar subtareas")
    
    parent = find_task_by_id(project.tasks, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Tarea padre no encontrada")
    
    # Validar que la tarea padre no esté completada
    if parent.status == "completed":
        raise HTTPException(
            status_code=400,
            detail="No se pueden agregar subtareas a una tarea completada. Debe reabrir la tarea primero."
        )
    
    # Obtener datos del formulario
    form = await request.form()
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip()
    
    if not title:
        raise HTTPException(status_code=400, detail="El título es requerido")
    
    # Crear la subtarea (sin archivos, se agregan después)
    child = Task(
        id=uuid.uuid4().hex[:10],
        title=title,
        description=description,
        status="pending",
        children=[],
        attachments=[]
    )
    
    parent.children.append(child)
    save_project(project)
    
    # Crear log automático
    create_task_log(project, child, "task_create", user)
    save_project(project)
    
    return JSONResponse({"status": "ok", "task_id": child.id})


def _find_parent_task(tasks: List[Task], task_id: str, parent: Optional[Task] = None) -> Optional[Task]:
    """Encuentra la tarea padre de una subtarea."""
    for task in tasks:
        if task.id == task_id:
            return parent
        found = _find_parent_task(task.children, task_id, task)
        if found is not None:
            return found
    return None


def create_task_log(project: Project, task: Task, action_type: str, user: dict, text: str = None):
    """Crea un log automático para una acción de tarea."""
    all_users = get_all_users()
    user_name = next((u.get("display_name") or u.get("username") for u in all_users if u.get("id") == user["id"]), user.get("username", "Usuario"))
    
    action_texts = {
        "task_start": f"{user_name} inició la tarea '{task.title}'",
        "task_pause": f"{user_name} pausó la tarea '{task.title}'",
        "task_complete": f"{user_name} completó la tarea '{task.title}'",
        "task_create": f"{user_name} creó la tarea '{task.title}'",
        "task_reopen": f"{user_name} reabrió la tarea '{task.title}'",
    }
    
    log_text = text or action_texts.get(action_type, f"{user_name} {action_type} en '{task.title}'")
    
    log = LogEntry(
        id=uuid.uuid4().hex[:10],
        text=log_text,
        created_at=_now_ts(),
        created_by=user["id"],
        task_id=task.id,
        action_type=action_type
    )
    project.logs.insert(0, log)


@router.patch("/api/tasks/{task_id}/status")
async def update_task_status(task_id: str, request: Request, data: dict = Body(...)):
    """Actualiza estado de tarea (start/pause/complete)."""
    user = require_user(request)
    project_id = data.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id requerido")
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    new_status = data.get("status", task.status)
    
    now = _now_ts()
    
    if new_status == "running" and task.status != "running":
        # Si tiene subtareas, NO se puede iniciar la tarea padre
        if task.children:
            raise HTTPException(
                status_code=400,
                detail="No se puede iniciar una tarea que tiene subtareas. Inicie las subtareas directamente."
            )
        else:
            # Verificar si es una subtarea y si el usuario puede iniciarla
            parent_task = _find_parent_task(project.tasks, task_id)
            if parent_task:
                # Es una subtarea: verificar permisos
                # Puede iniciar si:
                # 1. La subtarea no tiene asignación, O
                # 2. La subtarea está asignada al mismo usuario que la tarea padre
                can_start = False
                if not task.assigned_to:
                    # Subtarea sin asignación: verificar que el usuario esté asignado a la tarea padre
                    if parent_task.assigned_to and user["id"] in parent_task.assigned_to:
                        can_start = True
                elif user["id"] in task.assigned_to:
                    # Subtarea asignada al usuario: puede iniciar
                    can_start = True
                
                if not can_start:
                    raise HTTPException(
                        status_code=403,
                        detail="No tienes permiso para iniciar esta subtarea. Debes estar asignado a la tarea padre o a esta subtarea."
                    )
            else:
                # Es una tarea raíz: validar que el usuario esté asignado a la tarea
                if not task.assigned_to or user["id"] not in task.assigned_to:
                    raise HTTPException(
                        status_code=403,
                        detail="No tienes permiso para iniciar esta tarea. Debes estar asignado a la tarea."
                    )
            # Iniciar la tarea normalmente
            task.status = "running"
            task.started_at = now
    elif new_status == "paused":
        # Si tiene subtareas, NO se puede pausar la tarea padre directamente
        if task.children:
            raise HTTPException(
                status_code=400,
                detail="No se puede pausar una tarea que tiene subtareas. Pause las subtareas directamente."
            )
        else:
            # Verificar si es una subtarea y si el usuario puede pausarla
            parent_task = _find_parent_task(project.tasks, task_id)
            if parent_task:
                # Es una subtarea: verificar permisos
                # Puede pausar si:
                # 1. La subtarea no tiene asignación, O
                # 2. La subtarea está asignada al mismo usuario que la tarea padre
                can_pause = False
                if not task.assigned_to:
                    # Subtarea sin asignación: verificar que el usuario esté asignado a la tarea padre
                    if parent_task.assigned_to and user["id"] in parent_task.assigned_to:
                        can_pause = True
                elif user["id"] in task.assigned_to:
                    # Subtarea asignada al usuario: puede pausar
                    can_pause = True
                
                if not can_pause:
                    raise HTTPException(
                        status_code=403,
                        detail="No tienes permiso para pausar esta subtarea. Debes estar asignado a la tarea padre o a esta subtarea."
                    )
            else:
                # Es una tarea raíz: validar que el usuario esté asignado a la tarea
                if not task.assigned_to or user["id"] not in task.assigned_to:
                    raise HTTPException(
                        status_code=403,
                        detail="No tienes permiso para pausar esta tarea. Debes estar asignado a la tarea."
                    )
            # Si no tiene subtareas, pausar la tarea normalmente
            if task.status != "running":
                raise HTTPException(
                    status_code=400,
                    detail="La tarea no está corriendo, no se puede pausar."
                )
            if task.started_at:
                delta = int(now - task.started_at)
                task.accumulated_sec += delta
                task.started_at = None
            task.status = "paused"
            # Crear log automático
            create_task_log(project, task, "task_pause", user)
    elif new_status == "completed":
        # Validar que el usuario esté asignado a la tarea
        if not task.assigned_to or user["id"] not in task.assigned_to:
            raise HTTPException(
                status_code=403,
                detail="No tienes permiso para completar esta tarea. Debes estar asignado a la tarea."
            )
        
        # Validar que todas las subtareas estén completadas
        if task.children:
            incomplete_children = [c for c in task.children if c.status != "completed"]
            if incomplete_children:
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede completar la tarea. Hay {len(incomplete_children)} subtarea(s) sin completar."
                )
        
        # Validar que la tarea tenga tiempo acumulado (haya iniciado el temporizador)
        total_time = get_task_total_seconds(task, now)
        if total_time == 0:
            raise HTTPException(
                status_code=400,
                detail="No se puede completar la tarea. Debe iniciar el temporizador al menos una vez."
            )
        
        # Si está corriendo, acumular el tiempo antes de completar
        if task.status == "running" and task.started_at:
            delta = int(now - task.started_at)
            task.accumulated_sec += delta
            task.started_at = None
        
        # Obtener comentario si se proporcionó
        completion_comment = data.get("completion_comment", "").strip() or None
        task.completed_by = user["id"]
        task.completion_comment = completion_comment
        
        # Completar la tarea
        task.status = "completed"
        task.completed_at = now
        
        # Crear log automático con comentario si existe
        log_text = None
        if completion_comment:
            all_users = get_all_users()
            user_name = next((u.get("display_name") or u.get("username") for u in all_users if u.get("id") == user["id"]), user.get("username", "Usuario"))
            log_text = f"{user_name} completó la tarea '{task.title}'. Comentario: {completion_comment}"
        create_task_log(project, task, "task_complete", user, log_text)
    
    # Sincronizar completitud derivada
    sync_derived_completion(project.tasks)
    
    save_project(project)
    return JSONResponse({"status": "ok"})


@router.post("/api/tasks/{task_id}/reopen")
async def reopen_task(task_id: str, request: Request, data: dict = Body(...)):
    """Reabre una tarea completada con justificación. Solo usuarios asignados a la tarea."""
    user = require_user(request)
    project_id = data.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id requerido")
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    # Solo usuarios asignados a la tarea pueden reabrirla
    if not task.assigned_to or user["id"] not in task.assigned_to:
        raise HTTPException(status_code=403, detail="Solo los usuarios asignados a la tarea pueden reabrirla")
    
    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Solo se pueden reabrir tareas completadas")
    
    justification = (data.get("justification") or "").strip()
    # La justificación es opcional, usar un valor por defecto si no se proporciona
    if not justification:
        justification = "Tarea reabierta sin justificación específica"
    
    now = _now_ts()
    
    # Reabrir la tarea
    task.status = "pending"
    task.completed_at = None
    task.completed_by = None
    task.completion_comment = None
    task.completion_evidence = []
    
    # Reabrir también las subtareas
    def reopen_children(children):
        for child in children:
            if child.status == "completed":
                child.status = "pending"
                child.completed_at = None
                child.completed_by = None
                child.completion_comment = None
                child.completion_evidence = []
            if child.children:
                reopen_children(child.children)
    
    reopen_children(task.children)
    
    # Crear log automático con justificación
    all_users = get_all_users()
    user_name = next((u.get("display_name") or u.get("username") for u in all_users if u.get("id") == user["id"]), user.get("username", "Usuario"))
    log_text = f"{user_name} reabrió la tarea '{task.title}'. Justificación: {justification}"
    create_task_log(project, task, "task_reopen", user, log_text)
    
    save_project(project)
    return JSONResponse({"status": "ok"})


@router.post("/api/tasks/{task_id}/reopen_evidence")
async def upload_reopen_evidence(task_id: str, request: Request, project_id: str = Form(...)):
    """Sube archivos de soporte al reabrir una tarea (múltiples archivos). Solo usuarios asignados a la tarea."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    # Solo usuarios asignados a la tarea pueden subir evidencias de reapertura
    if not task.assigned_to or user["id"] not in task.assigned_to:
        raise HTTPException(status_code=403, detail="Solo los usuarios asignados a la tarea pueden subir archivos de reapertura")
    
    if task.status == "completed":
        raise HTTPException(status_code=400, detail="La tarea aún está completada. Debe reabrirse primero.")
    
    # Obtener archivos del form data
    form = await request.form()
    files = form.getlist("files")
    
    # Inicializar lista si no existe
    if not task.reopen_evidence:
        task.reopen_evidence = []
    
    # Guardar todos los archivos
    attachment_ids = []
    for file_item in files:
        if hasattr(file_item, 'file'):  # Es un UploadFile
            attachment = save_attachment(file_item, user["id"])
            task.reopen_evidence.append(attachment)
            attachment_ids.append(attachment.id)
    
    save_project(project)
    
    return JSONResponse({"status": "ok", "attachment_ids": attachment_ids})


@router.post("/api/tasks/{task_id}/attachments")
async def upload_task_attachments(task_id: str, request: Request, project_id: str = Form(...)):
    """Agrega archivos adjuntos a una tarea existente. Solo el creador del proyecto puede agregar archivos."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto puede agregar archivos a tareas
    if project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto puede agregar archivos a las tareas")
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    # Obtener archivos del form data
    form = await request.form()
    files = form.getlist("files")
    
    if not files:
        raise HTTPException(status_code=400, detail="No se proporcionaron archivos")
    
    # Inicializar lista si no existe
    if not task.attachments:
        task.attachments = []
    
    # Guardar todos los archivos
    attachment_ids = []
    for file_item in files:
        if hasattr(file_item, 'file'):  # Es un UploadFile
            try:
                attachment = save_attachment(file_item, user["id"])
                task.attachments.append(attachment)
                attachment_ids.append(attachment.id)
            except HTTPException:
                raise
            except Exception as e:
                # Si falla guardar un archivo, continuar con los demás
                pass
    
    save_project(project)
    
    # Crear log automático
    all_users = get_all_users()
    user_name = next((u.get("display_name") or u.get("username") for u in all_users if u.get("id") == user["id"]), user.get("username", "Usuario"))
    log_text = f"{user_name} agregó {len(attachment_ids)} archivo(s) a la tarea '{task.title}'"
    create_task_log(project, task, "task_attachment_added", user, log_text)
    save_project(project)
    
    return JSONResponse({"status": "ok", "attachment_ids": attachment_ids})


@router.delete("/api/tasks/{task_id}/attachments/{attachment_id}")
async def delete_task_attachment(task_id: str, attachment_id: str, request: Request, project_id: str = Query(...), attachment_type: str = Query("attachments")):
    """Elimina un archivo adjunto de una tarea (solo quien lo subió).
    
    attachment_type puede ser: 'attachments', 'completion_evidence', 'reopen_evidence'
    """
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    # Determinar de qué lista eliminar
    attachment_list = None
    if attachment_type == "attachments":
        attachment_list = task.attachments
    elif attachment_type == "completion_evidence":
        attachment_list = task.completion_evidence
    elif attachment_type == "reopen_evidence":
        attachment_list = task.reopen_evidence
    else:
        raise HTTPException(status_code=400, detail="Tipo de adjunto inválido")
    
    if not attachment_list:
        raise HTTPException(status_code=404, detail="No hay archivos adjuntos")
    
    # Buscar el archivo
    attachment_to_delete = None
    for att in attachment_list:
        if att.id == attachment_id:
            attachment_to_delete = att
            break
    
    if not attachment_to_delete:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    
    # Verificar que solo quien subió el archivo pueda eliminarlo
    if attachment_to_delete.uploaded_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo puedes eliminar archivos que hayas subido tú")
    
    # Eliminar el archivo de la lista
    if attachment_type == "attachments":
        task.attachments = [att for att in task.attachments if att.id != attachment_id]
    elif attachment_type == "completion_evidence":
        task.completion_evidence = [att for att in task.completion_evidence if att.id != attachment_id]
    elif attachment_type == "reopen_evidence":
        task.reopen_evidence = [att for att in task.reopen_evidence if att.id != attachment_id]
    
    # Eliminar el archivo físico
    try:
        file_path = ATTACHMENTS_DIR / attachment_to_delete.filename
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        # Si falla eliminar el archivo físico, continuar de todas formas
        pass
    
    save_project(project)
    
    # Crear log automático
    all_users = get_all_users()
    user_name = next((u.get("display_name") or u.get("username") for u in all_users if u.get("id") == user["id"]), user.get("username", "Usuario"))
    log_text = f"{user_name} eliminó el archivo '{attachment_to_delete.original_name}' de la tarea '{task.title}'"
    create_task_log(project, task, "task_attachment_deleted", user, log_text)
    save_project(project)
    
    return JSONResponse({"status": "ok"})


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, request: Request, project_id: str = Query(...)):
    """Elimina una tarea. Solo el creador del proyecto puede eliminar tareas."""
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto puede eliminar tareas
    if project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto puede eliminar tareas")
    
    if not remove_task_by_id(project.tasks, task_id):
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    save_project(project)
    return JSONResponse({"status": "ok"})


@router.patch("/api/tasks/{task_id}/reorder")
async def reorder_task(task_id: str, request: Request, data: dict = Body(...)):
    """Reordena tareas (arriba/abajo)."""
    project_id = data.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id requerido")
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    direction = data.get("direction", "down")  # "up" or "down"
    
    # Buscar tarea y su índice en el array padre
    def find_task_with_parent(tasks: List[Task], target_id: str, parent_list=None, index=None):
        if parent_list is None:
            parent_list = project.tasks
            for i, t in enumerate(parent_list):
                if t.id == target_id:
                    return t, parent_list, i
                found = find_task_with_parent(t.children, target_id, t.children, None)
                if found[0]:
                    return found
        else:
            for i, t in enumerate(tasks):
                if t.id == target_id:
                    return t, parent_list, i
                found = find_task_with_parent(t.children, target_id, t.children, None)
                if found[0]:
                    return found
        return None, None, None
    
    task, parent_list, idx = find_task_with_parent(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    if direction == "up" and idx > 0:
        parent_list[idx], parent_list[idx - 1] = parent_list[idx - 1], parent_list[idx]
    elif direction == "down" and idx < len(parent_list) - 1:
        parent_list[idx], parent_list[idx + 1] = parent_list[idx + 1], parent_list[idx]
    
    save_project(project)
    return JSONResponse({"status": "ok"})


# ============================================================
# API: Áreas y Grupos
# ============================================================

@router.post("/api/areas/create")
async def create_area(request: Request, data: dict = Body(...)):
    """Crea una nueva área."""
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")
    
    index = load_index()
    
    # Verificar duplicados
    if any(a.name.lower() == name.lower() for a in index.areas):
        raise HTTPException(status_code=400, detail="Ya existe un área con ese nombre")
    
    area = Area(
        id=uuid.uuid4().hex[:8],
        name=name,
        description=(data.get("description") or "").strip(),
        created_at=_now_ts(),
        created_by="user"
    )
    
    index.areas.append(area)
    save_index(index)
    
    return JSONResponse({"status": "ok", "area_id": area.id})


@router.post("/api/groups/create")
async def create_group(request: Request, data: dict = Body(...)):
    """Crea un nuevo grupo."""
    name = (data.get("name") or "").strip()
    area_id = (data.get("area_id") or DEFAULT_AREA_ID).strip()
    
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")
    
    index = load_index()
    
    # Verificar área existe
    if not any(a.id == area_id for a in index.areas):
        raise HTTPException(status_code=404, detail="Área no encontrada")
    
    # Verificar duplicados
    if any(g.name.lower() == name.lower() and g.area_id == area_id for g in index.groups):
        raise HTTPException(status_code=400, detail="Ya existe un grupo con ese nombre en esa área")
    
    group = Group(
        id=uuid.uuid4().hex[:8],
        name=name,
        area_id=area_id,
        description=(data.get("description") or "").strip(),
        created_at=_now_ts(),
        created_by="user"
    )
    
    index.groups.append(group)
    save_index(index)
    
    return JSONResponse({"status": "ok", "group_id": group.id})


@router.delete("/api/areas/{area_id}")
async def delete_area(area_id: str, request: Request):
    """Elimina un área y reasigna proyectos."""
    if area_id == DEFAULT_AREA_ID:
        raise HTTPException(status_code=400, detail="No se puede eliminar el área por defecto")
    
    index = load_index()
    area = next((a for a in index.areas if a.id == area_id), None)
    if not area:
        raise HTTPException(status_code=404, detail="Área no encontrada")
    
    # Reasignar proyectos a área por defecto
    all_projects = list_all_projects()
    for project in all_projects:
        if project.area_id == area_id:
            project.area_id = DEFAULT_AREA_ID
            # Buscar grupo por defecto en área general
            default_group = next((g for g in index.groups if g.area_id == DEFAULT_AREA_ID), None)
            if default_group:
                project.group_id = default_group.id
            else:
                project.group_id = DEFAULT_GROUP_ID
            save_project(project)
            index.projects[project.id] = {
                "area_id": project.area_id,
                "group_id": project.group_id
            }
    
    # Eliminar grupos del área
    index.groups = [g for g in index.groups if g.area_id != area_id]
    
    # Eliminar área
    index.areas = [a for a in index.areas if a.id != area_id]
    save_index(index)
    
    return JSONResponse({"status": "ok"})


@router.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, request: Request):
    """Elimina un grupo y reasigna proyectos."""
    if group_id == DEFAULT_GROUP_ID:
        raise HTTPException(status_code=400, detail="No se puede eliminar el grupo por defecto")
    
    index = load_index()
    group = next((g for g in index.groups if g.id == group_id), None)
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado")
    
    # Reasignar proyectos a grupo por defecto
    all_projects = list_all_projects()
    for project in all_projects:
        if project.group_id == group_id:
            project.group_id = DEFAULT_GROUP_ID
            save_project(project)
            index.projects[project.id] = {
                "area_id": project.area_id,
                "group_id": project.group_id
            }
    
    # Eliminar grupo
    index.groups = [g for g in index.groups if g.id != group_id]
    save_index(index)
    
    return JSONResponse({"status": "ok"})


# ============================================================
# API: Adjuntos y Logs
# ============================================================

@router.post("/api/projects/{project_id}/attachments")
async def upload_project_attachment(project_id: str, request: Request):
    """Sube múltiples adjuntos a un proyecto. Solo el creador del proyecto puede subir archivos."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto puede subir archivos al proyecto
    if project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto puede subir archivos al proyecto")
    
    form = await request.form()
    files = form.getlist("files")
    
    if not files:
        raise HTTPException(status_code=400, detail="No se proporcionaron archivos")
    
    attachment_ids = []
    for file_item in files:
        if hasattr(file_item, 'file'):  # Es un UploadFile
            try:
                attachment = save_attachment(file_item, user["id"])
                project.attachments.insert(0, attachment)
                attachment_ids.append(attachment.id)
            except HTTPException:
                raise
            except Exception as e:
                # Si falla guardar un archivo, continuar con los demás
                pass
    
    save_project(project)
    
    return JSONResponse({"status": "ok", "attachment_ids": attachment_ids})


@router.delete("/api/projects/{project_id}/attachments/{attachment_id}")
async def delete_project_attachment(project_id: str, attachment_id: str, request: Request):
    """Elimina un archivo adjunto de un proyecto (solo quien lo subió)."""
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    if not project.attachments:
        raise HTTPException(status_code=404, detail="No hay archivos adjuntos")
    
    # Buscar el archivo
    attachment_to_delete = None
    for att in project.attachments:
        if att.id == attachment_id:
            attachment_to_delete = att
            break
    
    if not attachment_to_delete:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    
    # Verificar que solo quien subió el archivo pueda eliminarlo
    if attachment_to_delete.uploaded_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo puedes eliminar archivos que hayas subido tú")
    
    # Eliminar el archivo de la lista
    project.attachments = [att for att in project.attachments if att.id != attachment_id]
    
    # Eliminar el archivo físico
    try:
        file_path = ATTACHMENTS_DIR / attachment_to_delete.filename
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        # Si falla eliminar el archivo físico, continuar de todas formas
        pass
    
    save_project(project)
    
    return JSONResponse({"status": "ok"})


@router.post("/api/projects/{project_id}/logs")
async def add_log(project_id: str, request: Request, data: dict = Body(...)):
    """Añade un log/bitácora."""
    user = require_user(request)
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Texto requerido")
    
    all_users = get_all_users()
    user_name = next((u.get("display_name") or u.get("username") for u in all_users if u.get("id") == user["id"]), user.get("username", "Usuario"))
    
    log = LogEntry(
        id=uuid.uuid4().hex[:10],
        text=f"{user_name}: {text}",
        created_at=_now_ts(),
        created_by=user["id"],
        action_type="log_entry"
    )
    
    project.logs.insert(0, log)
    save_project(project)
    
    return JSONResponse({"status": "ok", "log_id": log.id})


@router.post("/api/tasks/{task_id}/completion_evidence")
async def upload_completion_evidence(task_id: str, request: Request, project_id: str = Form(...)):
    """Sube evidencias al completar una tarea (múltiples archivos). Solo usuarios asignados a la tarea."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    # Solo usuarios asignados a la tarea pueden subir evidencias
    if not task.assigned_to or user["id"] not in task.assigned_to:
        raise HTTPException(status_code=403, detail="Solo los usuarios asignados a la tarea pueden subir evidencias")
    
    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Solo se pueden subir evidencias para tareas completadas")
    
    # Obtener archivos del form data
    form = await request.form()
    files = form.getlist("files")
    
    # Inicializar lista si no existe
    if not task.completion_evidence:
        task.completion_evidence = []
    
    # Guardar todos los archivos
    attachment_ids = []
    for file_item in files:
        if hasattr(file_item, 'file'):  # Es un UploadFile
            attachment = save_attachment(file_item, user["id"])
            task.completion_evidence.append(attachment)
            attachment_ids.append(attachment.id)
    
    save_project(project)
    
    return JSONResponse({"status": "ok", "attachment_ids": attachment_ids})


@router.get("/attachments/{filename}")
async def get_attachment(filename: str):
    """Sirve archivos adjuntos."""
    path = ATTACHMENTS_DIR / filename
    if not path.exists() or not str(path).startswith(str(ATTACHMENTS_DIR.resolve())):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(str(path), filename=filename)


# ============================================================
# API: Asignación de Usuarios
# ============================================================

@router.post("/api/projects/{project_id}/assign_users")
async def assign_users_to_project(project_id: str, request: Request, data: dict = Body(...)):
    """Asigna usuarios y/o equipos a un proyecto."""
    user = require_user(request)
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo admin o creador pueden asignar usuarios
    if not _is_admin(user) and project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="No tienes permisos para asignar usuarios")
    
    user_ids = data.get("user_ids", [])
    team_ids = data.get("team_ids", [])
    
    if not isinstance(user_ids, list):
        user_ids = []
    if not isinstance(team_ids, list):
        team_ids = []
    
    # Expandir equipos a sus miembros
    expanded_team_user_ids = expand_team_members(team_ids)
    
    # Combinar usuarios directos y usuarios de equipos
    all_user_ids = list(set(user_ids + expanded_team_user_ids))
    
    # Validar que los usuarios existen
    all_users = get_all_users()
    valid_user_ids = [u.get("id") for u in all_users if u.get("id") in all_user_ids]
    
    project.assigned_users = valid_user_ids
    save_project(project)
    
    return JSONResponse({
        "status": "ok", 
        "assigned_users": valid_user_ids,
        "expanded_from_teams": expanded_team_user_ids
    })


@router.post("/api/tasks/{task_id}/assign_users")
async def assign_users_to_task(task_id: str, request: Request, data: dict = Body(...)):
    """Asigna usuarios y/o equipos a una tarea. Solo el creador del proyecto puede asignar usuarios."""
    user = require_user(request)
    
    project_id = data.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id requerido")
    
    project = load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    
    # Solo el creador del proyecto puede asignar usuarios a tareas
    if project.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="Solo el creador del proyecto puede asignar usuarios a las tareas")
    
    task = find_task_by_id(project.tasks, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    
    user_ids = data.get("user_ids", [])
    team_ids = data.get("team_ids", [])
    
    if not isinstance(user_ids, list):
        user_ids = []
    if not isinstance(team_ids, list):
        team_ids = []
    
    # Expandir equipos a sus miembros
    expanded_team_user_ids = expand_team_members(team_ids)
    
    # Combinar usuarios directos y usuarios de equipos
    all_user_ids = list(set(user_ids + expanded_team_user_ids))
    
    # Validar que los usuarios existen
    all_users = get_all_users()
    valid_user_ids = [u.get("id") for u in all_users if u.get("id") in all_user_ids]
    
    # Validar que todos los usuarios estén asignados al proyecto
    project_assigned_users = set(project.assigned_users)
    invalid_user_ids = [uid for uid in valid_user_ids if uid not in project_assigned_users]
    
    if invalid_user_ids:
        all_users_dict = {u.get("id"): u for u in all_users}
        invalid_names = [all_users_dict.get(uid, {}).get("display_name") or all_users_dict.get(uid, {}).get("username", uid) for uid in invalid_user_ids]
        raise HTTPException(
            status_code=400,
            detail=f"No se pueden asignar usuarios que no estén en el proyecto: {', '.join(invalid_names)}"
        )
    
    task.assigned_to = valid_user_ids
    save_project(project)
    
    return JSONResponse({
        "status": "ok", 
        "assigned_users": valid_user_ids,
        "expanded_from_teams": expanded_team_user_ids
    })


# ============================================================
# API: Equipos (Teams)
# ============================================================

@router.get("/api/teams")
async def get_teams(request: Request):
    """Obtiene todos los equipos."""
    user = require_user(request)
    teams = load_teams()
    return JSONResponse({"teams": [t.model_dump() for t in teams]})


@router.get("/api/teams/{team_id}")
async def get_team_by_id(team_id: str, request: Request):
    """Obtiene un equipo por ID."""
    user = require_user(request)
    team = get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    return JSONResponse({"team": team.model_dump()})


@router.post("/api/teams/create")
async def create_team_endpoint(request: Request, data: dict = Body(...)):
    """Crea un nuevo equipo."""
    user = require_user(request)
    
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")
    
    description = (data.get("description") or "").strip()
    member_ids = data.get("member_ids", [])
    if not isinstance(member_ids, list):
        member_ids = []
    
    # Validar que los usuarios existen
    all_users = get_all_users()
    valid_user_ids = [u.get("id") for u in all_users if u.get("id") in member_ids]
    
    team = create_team(name, description, valid_user_ids, user["id"])
    return JSONResponse({"status": "ok", "team": team.model_dump()})


@router.post("/api/teams/{team_id}/update")
async def update_team_endpoint(team_id: str, request: Request, data: dict = Body(...)):
    """Actualiza un equipo."""
    user = require_user(request)
    
    team = get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    
    # Solo admin o creador pueden actualizar
    if not _is_admin(user) and team.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="No tienes permisos para actualizar este equipo")
    
    name = data.get("name")
    description = data.get("description")
    member_ids = data.get("member_ids")
    
    if member_ids is not None:
        if not isinstance(member_ids, list):
            member_ids = []
        # Validar que los usuarios existen
        all_users = get_all_users()
        member_ids = [u.get("id") for u in all_users if u.get("id") in member_ids]
    
    updated_team = update_team(team_id, name=name, description=description, member_ids=member_ids)
    if not updated_team:
        raise HTTPException(status_code=500, detail="Error actualizando equipo")
    
    return JSONResponse({"status": "ok", "team": updated_team.model_dump()})


@router.delete("/api/teams/{team_id}")
async def delete_team_endpoint(team_id: str, request: Request):
    """Elimina un equipo."""
    user = require_user(request)
    
    team = get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    
    # Solo admin o creador pueden eliminar
    if not _is_admin(user) and team.created_by != user["id"]:
        raise HTTPException(status_code=403, detail="No tienes permisos para eliminar este equipo")
    
    success = delete_team(team_id)
    if not success:
        raise HTTPException(status_code=500, detail="Error eliminando equipo")
    
    return JSONResponse({"status": "ok"})
