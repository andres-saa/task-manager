"""
Sistema de almacenamiento JSON local.
Estrategia: Índice JSON + Archivos individuales por proyecto.
"""
import os
import json
import uuid
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from filelock import FileLock

from models import Project, ProjectIndex, Area, Group, Task, LogEntry, Attachment, Team

# Caché simple en memoria
_index_cache: Optional[ProjectIndex] = None
_index_cache_time: float = 0
_index_cache_ttl: float = 2.0  # 2 segundos de caché

_projects_cache: Dict[str, tuple[Project, float]] = {}  # {project_id: (project, timestamp)}
_projects_cache_ttl: float = 5.0  # 5 segundos de caché


# Configuración
STORAGE_DIR = Path("data")
PROJECTS_DIR = STORAGE_DIR / "projects"
INDEX_FILE = STORAGE_DIR / "index.json"
LOCK_FILE = STORAGE_DIR / "index.json.lock"
TEAMS_FILE = STORAGE_DIR / "teams.json"
TEAMS_LOCK_FILE = STORAGE_DIR / "teams.json.lock"

# Crear directorios si no existen
STORAGE_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)

DEFAULT_AREA_ID = "general"
DEFAULT_GROUP_ID = "general"


def _now_ts() -> float:
    """Retorna timestamp actual."""
    return time.time()


def _read_json_file(path: Path, default: Any) -> Any:
    """Lee un archivo JSON de forma segura."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file(path: Path, data: Any) -> None:
    """Escribe un archivo JSON de forma segura (atomic write)."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_index(use_cache: bool = True) -> ProjectIndex:
    """Carga el índice global con caché."""
    global _index_cache, _index_cache_time
    
    # Usar caché si está disponible y no expirado
    if use_cache and _index_cache is not None:
        age = time.time() - _index_cache_time
        if age < _index_cache_ttl:
            return _index_cache
    
    # Limpiar lock file si existe y está bloqueado (más de 5 segundos)
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age > 5:  # Lock file tiene más de 5 segundos
                try:
                    LOCK_FILE.unlink()
                except Exception:
                    pass
        except Exception:
            pass
    
    try:
        lock = FileLock(str(LOCK_FILE), timeout=1)  # Reducido a 1 segundo
    except TypeError:
        lock = FileLock(str(LOCK_FILE))
    
    try:
        with lock:
            raw = _read_json_file(INDEX_FILE, {})
            
            # Normalizar estructura con manejo de errores
            areas = []
            for a in raw.get("areas", []):
                try:
                    areas.append(Area(**a))
                except Exception:
                    continue
            
            groups = []
            for g in raw.get("groups", []):
                try:
                    groups.append(Group(**g))
                except Exception:
                    continue
            
            projects = raw.get("projects", {})
            if not isinstance(projects, dict):
                projects = {}
            
            # Asegurar área y grupo por defecto
            needs_save = False
            if not any(a.id == DEFAULT_AREA_ID for a in areas):
                areas.insert(0, Area(
                    id=DEFAULT_AREA_ID,
                    name="General",
                    description="Área por defecto",
                    created_at=_now_ts(),
                    created_by="system"
                ))
                needs_save = True
            
            if not any(g.id == DEFAULT_GROUP_ID for g in groups):
                groups.insert(0, Group(
                    id=DEFAULT_GROUP_ID,
                    name="General",
                    area_id=DEFAULT_AREA_ID,
                    description="Grupo por defecto",
                    created_at=_now_ts(),
                    created_by="system"
                ))
                needs_save = True
            
            index = ProjectIndex(areas=areas, groups=groups, projects=projects)
            
            # Guardar solo si hubo cambios, pero fuera del lock para evitar deadlock
            if needs_save:
                data = {
                    "areas": [a.model_dump() for a in index.areas],
                    "groups": [g.model_dump() for g in index.groups],
                    "projects": index.projects
                }
                _write_json_file(INDEX_FILE, data)
            
            # Actualizar caché
            _index_cache = index
            _index_cache_time = time.time()
            return index
    except Exception as e:
        # Si hay error de lock, intentar sin lock (solo lectura)
        # Usar caché si está disponible
        if _index_cache is not None:
            return _index_cache
        raw = _read_json_file(INDEX_FILE, {})
        areas = [Area(**a) for a in raw.get("areas", []) if isinstance(a, dict)]
        groups = [Group(**g) for g in raw.get("groups", []) if isinstance(g, dict)]
        projects = raw.get("projects", {}) or {}
        
        if not any(a.id == DEFAULT_AREA_ID for a in areas):
            areas.insert(0, Area(
                id=DEFAULT_AREA_ID,
                name="General",
                description="Area por defecto",
                created_at=_now_ts(),
                created_by="system"
            ))
        
        if not any(g.id == DEFAULT_GROUP_ID for g in groups):
            groups.insert(0, Group(
                id=DEFAULT_GROUP_ID,
                name="General",
                area_id=DEFAULT_AREA_ID,
                description="Grupo por defecto",
                created_at=_now_ts(),
                created_by="system"
            ))
        
        index = ProjectIndex(areas=areas, groups=groups, projects=projects)
        # Actualizar caché
        _index_cache = index
        _index_cache_time = time.time()
        return index


def save_index(index: ProjectIndex) -> None:
    """Guarda el índice global."""
    global _index_cache, _index_cache_time
    
    # Limpiar lock file si tiene más de 5 segundos (probablemente bloqueado)
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age > 5:
                try:
                    LOCK_FILE.unlink()
                except Exception:
                    pass
        except Exception:
            pass
    
    try:
        try:
            lock = FileLock(str(LOCK_FILE), timeout=1)  # Reducido a 1 segundo
        except TypeError:
            lock = FileLock(str(LOCK_FILE))
        
        with lock:
            data = {
                "areas": [a.model_dump() for a in index.areas],
                "groups": [g.model_dump() for g in index.groups],
                "projects": index.projects
            }
            _write_json_file(INDEX_FILE, data)
            # Actualizar caché
            _index_cache = index
            _index_cache_time = time.time()
    except Exception as e:
        # Si no puede adquirir el lock, intentar escribir directamente
        try:
            if LOCK_FILE.exists():
                try:
                    LOCK_FILE.unlink()
                except Exception:
                    pass
            
            data = {
                "areas": [a.model_dump() for a in index.areas],
                "groups": [g.model_dump() for g in index.groups],
                "projects": index.projects
            }
            _write_json_file(INDEX_FILE, data)
            # Actualizar caché
            _index_cache = index
            _index_cache_time = time.time()
        except Exception as e2:
            raise


def load_project(project_id: str, use_cache: bool = True) -> Optional[Project]:
    """Carga un proyecto individual desde su archivo JSON con caché."""
    global _projects_cache
    
    # Usar caché si está disponible
    if use_cache and project_id in _projects_cache:
        project, cache_time = _projects_cache[project_id]
        age = time.time() - cache_time
        if age < _projects_cache_ttl:
            return project
    
    project_file = PROJECTS_DIR / f"{project_id}.json"
    if not project_file.exists():
        return None
    
    raw = _read_json_file(project_file, None)
    if not raw:
        return None
    
    try:
        project = Project(**raw)
        # Actualizar caché
        _projects_cache[project_id] = (project, time.time())
        return project
    except Exception:
        return None


def save_project(project: Project) -> None:
    """Guarda un proyecto individual en su archivo JSON."""
    global _projects_cache
    
    # Asegurar que el directorio existe
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    
    project_file = PROJECTS_DIR / f"{project.id}.json"
    try:
        data = project.model_dump()
        _write_json_file(project_file, data)
        # Actualizar caché
        _projects_cache[project.id] = (project, time.time())
    except Exception as e:
        raise


def delete_project(project_id: str) -> bool:
    """Elimina el archivo de un proyecto."""
    project_file = PROJECTS_DIR / f"{project_id}.json"
    if project_file.exists():
        try:
            project_file.unlink()
            return True
        except Exception:
            return False
    return False


def list_all_projects() -> List[Project]:
    """Lista todos los proyectos cargando sus archivos individuales."""
    projects = []
    try:
        if not PROJECTS_DIR.exists():
            PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
            return projects
        
        # Obtener lista de archivos una sola vez
        project_files = list(PROJECTS_DIR.glob("*.json"))
        
        for project_file in project_files:
            project_id = project_file.stem
            try:
                project = load_project(project_id, use_cache=True)
                if project:
                    projects.append(project)
            except Exception:
                continue
    except Exception:
        pass
    
    return projects


def normalize_project(project_data: Dict[str, Any]) -> Project:
    """Normaliza un proyecto asegurando estructura correcta."""
    # Asegurar ID
    if "id" not in project_data:
        project_data["id"] = uuid.uuid4().hex[:12]
    
    # Asegurar arrays vacíos
    project_data.setdefault("tasks", [])
    project_data.setdefault("logs", [])
    project_data.setdefault("attachments", [])
    
    # Normalizar fechas
    if "deadline" in project_data and isinstance(project_data["deadline"], str):
        # Si viene como string, intentar parsear
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(project_data["deadline"].replace("Z", "+00:00"))
            project_data["deadline"] = dt.timestamp()
        except Exception:
            project_data["deadline"] = None
    
    # Normalizar tareas recursivamente
    project_data["tasks"] = [normalize_task(t) for t in project_data.get("tasks", [])]
    
    return Project(**project_data)


def normalize_task(task_data: Dict[str, Any]) -> Task:
    """Normaliza una tarea recursivamente."""
    # Asegurar ID
    if "id" not in task_data:
        task_data["id"] = uuid.uuid4().hex[:10]
    
    # Calcular segundos acumulados si hay started_at
    if "started_at" in task_data and task_data["started_at"]:
        if task_data.get("status") == "running":
            # Calcular delta si está corriendo
            started = task_data["started_at"]
            accumulated = task_data.get("accumulated_sec", 0)
            delta = int(_now_ts() - started)
            task_data["accumulated_sec"] = accumulated + delta
    
    # Normalizar estado
    status = task_data.get("status", "pending")
    if status not in ["pending", "running", "paused", "completed"]:
        task_data["status"] = "pending"
    
    # Migrar completion_evidence de Attachment único a lista
    if "completion_evidence" in task_data:
        if task_data["completion_evidence"] is None:
            task_data["completion_evidence"] = []
        elif isinstance(task_data["completion_evidence"], dict):
            # Es un Attachment único, convertirlo a lista
            task_data["completion_evidence"] = [normalize_attachment(task_data["completion_evidence"])]
        elif isinstance(task_data["completion_evidence"], list):
            # Ya es una lista, normalizar cada elemento
            task_data["completion_evidence"] = [normalize_attachment(a) if isinstance(a, dict) else a for a in task_data["completion_evidence"]]
    
    # Normalizar reopen_evidence si existe
    if "reopen_evidence" in task_data:
        if task_data["reopen_evidence"] is None:
            task_data["reopen_evidence"] = []
        elif isinstance(task_data["reopen_evidence"], dict):
            task_data["reopen_evidence"] = [normalize_attachment(task_data["reopen_evidence"])]
        elif isinstance(task_data["reopen_evidence"], list):
            task_data["reopen_evidence"] = [normalize_attachment(a) if isinstance(a, dict) else a for a in task_data["reopen_evidence"]]
    
    # Migrar attachment (legacy) a attachments (lista)
    if "attachment" in task_data and task_data["attachment"] is not None:
        # Si existe el campo legacy, migrarlo a la lista
        if "attachments" not in task_data or not task_data["attachments"]:
            task_data["attachments"] = []
        if isinstance(task_data["attachment"], dict):
            task_data["attachments"].append(normalize_attachment(task_data["attachment"]))
        # Eliminar el campo legacy
        del task_data["attachment"]
    
    # Normalizar attachments si existe
    if "attachments" in task_data:
        if task_data["attachments"] is None:
            task_data["attachments"] = []
        elif isinstance(task_data["attachments"], list):
            task_data["attachments"] = [normalize_attachment(a) if isinstance(a, dict) else a for a in task_data["attachments"]]
        else:
            task_data["attachments"] = []
    else:
        task_data["attachments"] = []
    
    # Normalizar children recursivamente
    task_data.setdefault("children", [])
    task_data["children"] = [normalize_task(c) for c in task_data.get("children", [])]
    
    return Task(**task_data)


def normalize_attachment(attach_data: Dict[str, Any]) -> Attachment:
    """Normaliza un adjunto (compatibilidad legacy)."""
    # Si es formato legacy (solo imagen)
    if "url" not in attach_data and "filename" in attach_data:
        attach_data["url"] = f"/attachments/{attach_data['filename']}"
    
    return Attachment(**attach_data)


def find_task_by_id(tasks: List[Task], task_id: str) -> Optional[Task]:
    """Busca una tarea recursivamente en el árbol."""
    for task in tasks:
        if task.id == task_id:
            return task
        found = find_task_by_id(task.children, task_id)
        if found:
            return found
    return None


def remove_task_by_id(tasks: List[Task], task_id: str) -> bool:
    """Elimina una tarea recursivamente del árbol."""
    for i, task in enumerate(tasks):
        if task.id == task_id:
            tasks.pop(i)
            return True
        if remove_task_by_id(task.children, task_id):
            return True
    return False


def calculate_progress(tasks: List[Task]) -> float:
    """Calcula porcentaje de progreso (tareas completadas / total)."""
    if not tasks:
        return 100.0
    
    def count_tasks(ts: List[Task]) -> tuple[int, int]:
        total = 0
        completed = 0
        for t in ts:
            total += 1
            if t.status == "completed":
                completed += 1
            sub_total, sub_completed = count_tasks(t.children)
            total += sub_total
            completed += sub_completed
        return total, completed
    
    total, completed = count_tasks(tasks)
    if total == 0:
        return 100.0
    return round((completed / total) * 100, 1)


def total_sec_from_tasks(tasks: List[Task]) -> int:
    """Suma recursiva de segundos acumulados de todas las tareas."""
    total = 0
    for task in tasks:
        total += task.accumulated_sec
        total += total_sec_from_tasks(task.children)
    return total


def total_sec_from_tasks_with_running(tasks: List[Task], now: Optional[float] = None) -> int:
    """Suma recursiva de segundos acumulados de todas las tareas, incluyendo timers activos."""
    if not now:
        now = _now_ts()
    total = 0
    for task in tasks:
        # Si tiene subtareas, solo sumar el tiempo de las subtareas (no el padre)
        if task.children:
            # Recursivamente sumar todas las subtareas
            total += total_sec_from_tasks_with_running(task.children, now)
        else:
            # Si no tiene subtareas, usar su tiempo propio + tiempo activo si está corriendo
            task_total = task.accumulated_sec
            if task.status == "running" and task.started_at:
                delta = int(now - task.started_at)
                task_total += delta
            total += task_total
    return total


def has_running_tasks(tasks: List[Task]) -> bool:
    """Verifica si hay alguna tarea corriendo (recursivamente)."""
    for task in tasks:
        if task.status == "running":
            return True
        if task.children and has_running_tasks(task.children):
            return True
    return False


def get_task_total_seconds(task: Task, now: Optional[float] = None) -> int:
    """Calcula el tiempo total de una tarea: si tiene subtareas, suma sus tiempos; si no, usa su tiempo propio."""
    if not now:
        now = _now_ts()
    
    # Si tiene subtareas, sumar el tiempo de todas las subtareas
    if task.children:
        total = 0
        for child in task.children:
            # Tiempo acumulado de la subtarea
            child_total = child.accumulated_sec
            
            # Si está corriendo, agregar el tiempo transcurrido
            if child.status == "running" and child.started_at:
                delta = int(now - child.started_at)
                child_total += delta
            
            total += child_total
        return total
    else:
        # Si no tiene subtareas, usar su tiempo propio
        total = task.accumulated_sec
        if task.status == "running" and task.started_at:
            delta = int(now - task.started_at)
            total += delta
        return total


def sync_derived_completion(tasks: List[Task]) -> None:
    """Si todos los hijos están completos, marca el padre como completo."""
    for task in tasks:
        if task.children:
            sync_derived_completion(task.children)
            # Si todos los hijos están completos y el padre no lo está
            if all(c.status == "completed" for c in task.children):
                if task.status != "completed":
                    task.status = "completed"
                    task.completed_at = _now_ts()


# ============================================================
# Equipos (Teams)
# ============================================================

def load_teams() -> List[Team]:
    """Carga todos los equipos."""
    try:
        lock = FileLock(str(TEAMS_LOCK_FILE), timeout=1)
    except TypeError:
        lock = FileLock(str(TEAMS_LOCK_FILE))
    
    try:
        with lock:
            raw = _read_json_file(TEAMS_FILE, [])
            teams = []
            for t in raw:
                try:
                    teams.append(Team(**t))
                except Exception:
                    continue
            return teams
    except Exception:
        return []


def save_teams(teams: List[Team]) -> None:
    """Guarda todos los equipos."""
    try:
        lock = FileLock(str(TEAMS_LOCK_FILE), timeout=1)
    except TypeError:
        lock = FileLock(str(TEAMS_LOCK_FILE))
    
    try:
        with lock:
            data = [t.model_dump() for t in teams]
            _write_json_file(TEAMS_FILE, data)
    except Exception as e:
        raise


def get_team(team_id: str) -> Optional[Team]:
    """Obtiene un equipo por ID."""
    teams = load_teams()
    for team in teams:
        if team.id == team_id:
            return team
    return None


def create_team(name: str, description: str, member_ids: List[str], created_by: str) -> Team:
    """Crea un nuevo equipo."""
    teams = load_teams()
    team_id = uuid.uuid4().hex[:12]
    
    team = Team(
        id=team_id,
        name=name.strip(),
        description=description.strip(),
        member_ids=member_ids,
        created_at=_now_ts(),
        created_by=created_by
    )
    
    teams.append(team)
    save_teams(teams)
    return team


def update_team(team_id: str, name: Optional[str] = None, description: Optional[str] = None, member_ids: Optional[List[str]] = None) -> Optional[Team]:
    """Actualiza un equipo."""
    teams = load_teams()
    for i, team in enumerate(teams):
        if team.id == team_id:
            if name is not None:
                team.name = name.strip()
            if description is not None:
                team.description = description.strip()
            if member_ids is not None:
                team.member_ids = member_ids
            teams[i] = team
            save_teams(teams)
            return team
    return None


def delete_team(team_id: str) -> bool:
    """Elimina un equipo."""
    teams = load_teams()
    original_count = len(teams)
    teams = [t for t in teams if t.id != team_id]
    if len(teams) < original_count:
        save_teams(teams)
        return True
    return False


def expand_team_members(team_ids: List[str]) -> List[str]:
    """Expande los IDs de equipos a los IDs de sus miembros."""
    if not team_ids:
        return []
    
    teams = load_teams()
    team_dict = {t.id: t for t in teams}
    user_ids = set()
    
    for team_id in team_ids:
        team = team_dict.get(team_id)
        if team:
            user_ids.update(team.member_ids)
    
    return list(user_ids)
