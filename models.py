"""
Modelos Pydantic para el sistema de gestión de proyectos.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class Attachment(BaseModel):
    """Modelo para archivos adjuntos."""
    id: str
    filename: str
    original_name: str
    url: str
    size: int
    content_type: str
    uploaded_at: float
    uploaded_by: str


class LogEntry(BaseModel):
    """Modelo para entradas de bitácora/logs."""
    id: str
    text: str
    created_at: float
    created_by: str
    attachment: Optional[Attachment] = None
    task_id: Optional[str] = None  # ID de la tarea relacionada
    action_type: Optional[str] = None  # 'task_start', 'task_pause', 'task_complete', 'task_create', 'log_entry', etc.


class Task(BaseModel):
    """Modelo recursivo para tareas y subtareas."""
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending, running, paused, completed
    accumulated_sec: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    completed_by: Optional[str] = None  # ID del usuario que completó la tarea
    completion_comment: Optional[str] = None  # Comentario al completar
    completion_evidence: List[Attachment] = Field(default_factory=list)  # Lista de evidencias adjuntas al completar
    assigned_to: List[str] = Field(default_factory=list)  # Lista de user_ids
    children: List["Task"] = Field(default_factory=list)
    attachments: List[Attachment] = Field(default_factory=list)  # Lista de archivos adjuntos a la tarea
    reopen_evidence: List[Attachment] = Field(default_factory=list)  # Lista de archivos al reabrir

    class Config:
        json_schema_extra = {
            "example": {
                "id": "task_123",
                "title": "Implementar feature X",
                "status": "running",
                "accumulated_sec": 3600,
                "children": []
            }
        }


# Permitir recursividad en Task
Task.model_rebuild()


class Project(BaseModel):
    """Modelo principal para proyectos."""
    id: str
    name: str
    description: str = ""
    area_id: str
    group_id: str
    deadline: Optional[float] = None  # timestamp
    created_at: float
    created_by: str
    assigned_users: List[str] = Field(default_factory=list)  # Lista de user_ids
    tasks: List[Task] = Field(default_factory=list)
    logs: List[LogEntry] = Field(default_factory=list)
    attachments: List[Attachment] = Field(default_factory=list)
    dirty: bool = False  # Flag para sincronización


class Area(BaseModel):
    """Modelo para Áreas."""
    id: str
    name: str
    description: str = ""
    created_at: float
    created_by: str


class Group(BaseModel):
    """Modelo para Grupos dentro de Áreas."""
    id: str
    name: str
    area_id: str
    description: str = ""
    created_at: float
    created_by: str


class Team(BaseModel):
    """Modelo para Equipos de trabajo."""
    id: str
    name: str
    description: str = ""
    member_ids: List[str] = Field(default_factory=list)  # Lista de user_ids
    created_at: float
    created_by: str


class ProjectIndex(BaseModel):
    """Índice global que mapea proyectos a áreas/grupos."""
    areas: List[Area] = Field(default_factory=list)
    groups: List[Group] = Field(default_factory=list)
    projects: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    # projects: {project_id: {"area_id": "...", "group_id": "..."}}
