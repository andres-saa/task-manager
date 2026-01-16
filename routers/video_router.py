import os
import json
import uuid
import cv2
import shutil
import secrets
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, UploadFile, File, Request, Form, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from filelock import FileLock


# ============================================================
# CONFIG
# ============================================================
router = APIRouter(prefix="/videos", tags=["Videos"])
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = Path("uploads")          # videos
THUMB_DIR = Path("thumbnails")        # thumbs videos
ATTACH_DIR = Path("attachments")      # adjuntos escuelas/cursos/clases
COLORS_FILE = Path("colors.json")     # colores por extensión

DB_FILE = Path("db.json")
LOCK_FILE = Path("db.json.lock")

BOGOTA_TZ = timezone(timedelta(hours=-5))

UPLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)
ATTACH_DIR.mkdir(exist_ok=True)

DEFAULT_SCHOOL_ID = "general"
DEFAULT_COURSE_ID = "general"
SESSION_COOKIE = "sm_sid"

# ---- Sync employees/users ----
EMPLOYERS_URL = "https://backend.salchimonster.com/employers-basic"
EMPLOYERS_SYNC_TTL_SECONDS = 3600

# ---- Seguridad ----
BOOTSTRAP_ADMIN_USER = os.getenv("ADMIN_USER", "andrew19f")
BOOTSTRAP_ADMIN_PASS = os.getenv("ADMIN_PASS", "change-me-please")

# Adjuntos: tamaño máximo
MAX_ATTACH_MB = float(os.getenv("MAX_ATTACH_MB", "25"))
MAX_ATTACH_BYTES = int(MAX_ATTACH_MB * 1024 * 1024)

# Extensiones permitidas (puedes ampliar)
ALLOWED_ATTACH_EXT = {
    "pdf", "png", "jpg", "jpeg", "webp",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv",
    "zip", "rar",
    "mp3", "wav",
    "mp4", "mov",
    "py", "js", "html", "css", "json", "sql"
}


# ============================================================
# JSON helpers + schema
# ============================================================
def _now_ts() -> float:
    return time.time()


def _read_json_file_nolock(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file_nolock(path: Path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --- CACHE DE COLORES ---
COLORS_CACHE: Dict[str, Dict[str, str]] = {}


def load_colors() -> Dict[str, Dict[str, str]]:
    """Carga colors.json en memoria si no está cargado."""
    global COLORS_CACHE
    if not COLORS_CACHE and COLORS_FILE.exists():
        try:
            with open(COLORS_FILE, "r", encoding="utf-8") as f:
                COLORS_CACHE = json.load(f) or {}
        except Exception:
            COLORS_CACHE = {}
    return COLORS_CACHE


def _pbkdf2_hash_password(password: str, salt_hex: Optional[str] = None) -> Tuple[str, str]:
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, 200_000)
    return salt_hex, dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    _, calc = _pbkdf2_hash_password(password, salt_hex=salt_hex)
    return secrets.compare_digest(calc, hash_hex)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _audit(db: Dict[str, Any], action: str, actor_user_id: str, meta: Dict[str, Any]):
    db.setdefault("audit", [])
    db["audit"].append({
        "ts": _now_ts(),
        "action": action,
        "actor_user_id": actor_user_id,
        "meta": meta
    })


def _ensure_entity_defaults(entity: Dict[str, Any]):
    entity.setdefault("description", "")
    entity.setdefault("attachments", [])  # list[{id, filename, original_name, url, size, content_type, uploaded_at, uploaded_by}]


# ============================================================
# ✅ PASSWORD RULE
# clave SIEMPRE: cedula al revés + '---' + cedula normal
# Ej: '123' => '321---123'
# ============================================================
def _password_from_dni(dni: str) -> str:
    d = str(dni or "").strip()
    return f"{d[::-1]}---{d}" if d else ""


def _normalize_site(s: str) -> str:
    return (s or "").strip().upper()


def _normalize_role(s: str) -> str:
    return (s or "").strip().upper()


def _is_admin(user: Dict[str, Any]) -> bool:
    r = (user or {}).get("role") or ""
    return str(r).strip().upper() in ("ADMIN", "MANAGER")


def _count_admins(db: Dict[str, Any]) -> int:
    return sum(1 for u in (db.get("users") or []) if u.get("is_active", True) and _is_admin(u))


def _ensure_db_schema(db_raw: Any) -> Dict[str, Any]:
    now = _now_ts()

    if isinstance(db_raw, dict):
        db = db_raw
        db.setdefault("schools", [])
        db.setdefault("projects", [])
        db.setdefault("videos", [])
        db.setdefault("users", [])
        db.setdefault("sessions", {})
        db.setdefault("enrollments", [])
        db.setdefault("watch_progress", {})
        db.setdefault("comments", [])
        db.setdefault("audit", [])
        db.setdefault("employers_last_sync_ts", 0)
        db.setdefault("employers_last_hash", "")

        # defaults base
        if not any(s.get("id") == DEFAULT_SCHOOL_ID for s in db["schools"]):
            db["schools"].insert(0, {
                "id": DEFAULT_SCHOOL_ID, "name": "General", "description": "", "attachments": [],
                "created_at": now, "created_by": "system",
            })

        if not any(p.get("id") == DEFAULT_COURSE_ID for p in db["projects"]):
            db["projects"].insert(0, {
                "id": DEFAULT_COURSE_ID, "name": "General", "school_id": DEFAULT_SCHOOL_ID, "description": "",
                "attachments": [], "created_at": now, "created_by": "system",
            })

        # normalize schools/courses/videos/users
        for s in db["schools"]:
            s.setdefault("created_at", now)
            s.setdefault("created_by", "system")
            _ensure_entity_defaults(s)

        for c in db["projects"]:
            c.setdefault("school_id", DEFAULT_SCHOOL_ID)
            c.setdefault("created_at", now)
            c.setdefault("created_by", "system")
            _ensure_entity_defaults(c)

        for v in db["videos"]:
            if not v.get("project_id"):
                v["project_id"] = DEFAULT_COURSE_ID
            v.setdefault("views", 0)
            v.setdefault("timestamp", 0)
            v.setdefault("created_at", v.get("created_at", now))
            v.setdefault("created_by", v.get("created_by", "system"))
            _ensure_entity_defaults(v)

        for u in db["users"]:
            u.setdefault("role", "EMPLEADO")
            u.setdefault("is_active", True)
            u.setdefault("created_at", u.get("created_at", now))
            u.setdefault("created_by", u.get("created_by", "system"))
            u.setdefault("dni", u.get("dni", u.get("username")))
            u.setdefault("gender", "")
            u.setdefault("position", u.get("position", u.get("role")))
            u.setdefault("site_name", u.get("site_name", ""))
            u.setdefault("external_id", u.get("external_id", None))
            u.setdefault("password_sig", u.get("password_sig", ""))
            # ✅ NUEVO: si le cambias rol manualmente, puedes “bloquear” el rol para que SYNC no lo pise
            u.setdefault("role_locked", bool(u.get("role_locked", False)))

        # normalize enrollments
        if not isinstance(db.get("enrollments"), list):
            db["enrollments"] = []
        for e in db["enrollments"]:
            if not isinstance(e, dict):
                continue
            e.setdefault("id", uuid.uuid4().hex[:10])
            e.setdefault("enrolled_at", float(e.get("enrolled_at", 0) or now))
            e.setdefault("enrolled_by", e.get("enrolled_by", "system"))
            e.setdefault("mode", e.get("mode", "unknown"))

        if not isinstance(db.get("comments"), list):
            db["comments"] = []

        return db

    # fallback
    return {
        "schools": [{
            "id": DEFAULT_SCHOOL_ID, "name": "General", "description": "", "attachments": [],
            "created_at": now, "created_by": "system"
        }],
        "projects": [{
            "id": DEFAULT_COURSE_ID, "name": "General", "school_id": DEFAULT_SCHOOL_ID,
            "description": "", "attachments": [], "created_at": now, "created_by": "system"
        }],
        "videos": [], "users": [], "sessions": {}, "enrollments": [],
        "watch_progress": {}, "comments": [], "audit": [],
        "employers_last_sync_ts": 0, "employers_last_hash": "",
    }


def load_db() -> Dict[str, Any]:
    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        _write_json_file_nolock(DB_FILE, db)
        return db


def save_db(db: Dict[str, Any]) -> None:
    with FileLock(str(LOCK_FILE)):
        _write_json_file_nolock(DB_FILE, db)


def _today_bogota_str() -> str:
    return datetime.now(BOGOTA_TZ).date().isoformat()


# ============================================================
# Find helpers
# ============================================================
def _find_school(db: Dict[str, Any], school_id: str) -> Optional[Dict[str, Any]]:
    for s in db.get("schools") or []:
        if s.get("id") == school_id:
            return s
    return None


def _all_schools_sorted(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    schools = list(db.get("schools") or [])
    schools.sort(key=lambda s: (0 if s.get("id") == DEFAULT_SCHOOL_ID else 1, float(s.get("created_at", 0) or 0)))
    return schools


def _find_course(db: Dict[str, Any], course_id: str) -> Optional[Dict[str, Any]]:
    for c in db.get("projects") or []:
        if c.get("id") == course_id:
            return c
    return None


def _all_courses_sorted(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    courses = list(db.get("projects") or [])
    courses.sort(key=lambda c: (
        0 if c.get("id") == DEFAULT_COURSE_ID else 1,
        str(c.get("school_id") or ""),
        float(c.get("created_at", 0) or 0),
    ))
    return courses


def _courses_for_school(db: Dict[str, Any], school_id: str) -> List[Dict[str, Any]]:
    courses = [c for c in (db.get("projects") or []) if (c.get("school_id") or DEFAULT_SCHOOL_ID) == (school_id or DEFAULT_SCHOOL_ID)]
    courses.sort(key=lambda c: (0 if c.get("id") == DEFAULT_COURSE_ID else 1, float(c.get("created_at", 0) or 0)))
    return courses


def _find_user_by_username(db: Dict[str, Any], username: str) -> Optional[Dict[str, Any]]:
    ukey = (username or "").strip().lower()
    for u in db.get("users") or []:
        if (u.get("username") or "").strip().lower() == ukey:
            return u
    return None


def _find_user(db: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    for u in db.get("users") or []:
        if u.get("id") == user_id:
            return u
    return None


def _is_enrolled(db: Dict[str, Any], user_id: str, course_id: str) -> bool:
    for e in db.get("enrollments") or []:
        if e.get("user_id") == user_id and e.get("course_id") == course_id:
            return True
    return False


def _filter_employees(db: Dict[str, Any], site_name: Optional[str] = None, role: Optional[str] = None) -> List[Dict[str, Any]]:
    site_key = _normalize_site(site_name) if site_name else None
    role_key = _normalize_role(role) if role else None

    out = []
    for u in db.get("users") or []:
        if not u.get("is_active", True):
            continue
        if _is_admin(u):
            continue

        if site_key:
            if _normalize_site(u.get("site_name") or "") != site_key:
                continue
        if role_key:
            if _normalize_role(u.get("role") or "") != role_key:
                continue
        out.append(u)
    return out


# ============================================================
# SYNC: empleados -> users
# ============================================================
def _normalize_employees_payload(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, list):
        return []

    out: List[Dict[str, Any]] = []
    for it in payload:
        if not isinstance(it, dict):
            continue

        dni = str(it.get("dni") or "").strip()
        if not dni:
            continue

        password_plain = _password_from_dni(dni)

        out.append({
            "external_id": it.get("id"),
            "dni": dni,
            "username": dni,
            "display_name": (it.get("name") or "").strip() or dni,
            "gender": (it.get("gender") or "").strip(),
            "position": (it.get("position") or "").strip(),
            "role": _normalize_role(it.get("position") or "EMPLEADO") or "EMPLEADO",
            "site_name": (it.get("site_name") or "").strip(),
            "password_plain": password_plain,
        })

    by_dni: Dict[str, Dict[str, Any]] = {}
    for x in out:
        by_dni[x["dni"]] = x

    final = list(by_dni.values())
    final.sort(key=lambda x: (x.get("dni"), str(x.get("external_id"))))
    return final


def _hash_employees_for_cache(items: List[Dict[str, Any]]) -> str:
    clean = []
    for x in items:
        clean.append({
            "external_id": x.get("external_id"),
            "dni": x.get("dni"),
            "display_name": x.get("display_name"),
            "gender": x.get("gender"),
            "role": x.get("role"),
            "site_name": x.get("site_name"),
        })
    raw = json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bootstrap_admin_if_needed(db: Dict[str, Any]):
    # si ya existe algún admin/manager activo, no crear
    for u in db.get("users") or []:
        if u.get("is_active", True) and _is_admin(u):
            return

    uid = uuid.uuid4().hex[:12]
    salt, pw_hash = _pbkdf2_hash_password(BOOTSTRAP_ADMIN_PASS)
    db["users"].append({
        "id": uid,
        "username": BOOTSTRAP_ADMIN_USER,
        "display_name": "Admin",
        "role": "ADMIN",
        "role_locked": True,  # ✅ que SYNC no lo pise nunca
        "is_active": True,
        "salt": salt,
        "password_hash": pw_hash,
        "password_sig": _sha256_hex(BOOTSTRAP_ADMIN_PASS),
        "dni": "",
        "gender": "",
        "position": "ADMIN",
        "site_name": "",
        "external_id": None,
        "created_at": _now_ts(),
        "created_by": "system",
    })
    _audit(db, "user_bootstrap_admin_created", "system", {"username": BOOTSTRAP_ADMIN_USER})


def sync_users_if_stale(force: bool = False) -> Dict[str, Any]:
    now = _now_ts()

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        _bootstrap_admin_if_needed(db)

        last = float(db.get("employers_last_sync_ts", 0) or 0)
        if (not force) and (now - last) < EMPLOYERS_SYNC_TTL_SECONDS:
            _write_json_file_nolock(DB_FILE, db)
            return {"status": "fresh", "last_sync_ts": last, "users_count": len(db.get("users") or [])}

        import urllib.request
        try:
            req = urllib.request.Request(
                EMPLOYERS_URL,
                headers={"User-Agent": "SalchiManager/2.0"}
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            payload = json.loads(body)
        except Exception as e:
            db["employers_last_sync_ts"] = now
            _write_json_file_nolock(DB_FILE, db)
            return {"status": "error", "error": str(e), "last_sync_ts": last}

        employees = _normalize_employees_payload(payload)
        payload_hash = _hash_employees_for_cache(employees)
        old_hash = db.get("employers_last_hash") or ""

        users = db.get("users") or []
        by_username: Dict[str, Dict[str, Any]] = {}
        for u in users:
            by_username[(u.get("username") or "").strip()] = u

        incoming_usernames = set()
        added: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        pw_updated_count = 0

        for emp in employees:
            uname = (emp.get("username") or "").strip()
            incoming_usernames.add(uname)

            existing = by_username.get(uname)
            password_plain = emp.get("password_plain") or ""
            pw_sig = _sha256_hex(password_plain) if password_plain else _sha256_hex("")

            if not existing:
                uid = uuid.uuid4().hex[:12]
                salt, pw_hash = _pbkdf2_hash_password(password_plain)
                new_user = {
                    "id": uid,
                    "username": uname,
                    "display_name": emp.get("display_name") or uname,
                    "role": emp.get("role") or "EMPLEADO",
                    "role_locked": False,
                    "is_active": True,
                    "salt": salt,
                    "password_hash": pw_hash,
                    "password_sig": pw_sig,
                    "dni": emp.get("dni") or uname,
                    "gender": emp.get("gender") or "",
                    "position": emp.get("position") or emp.get("role") or "",
                    "site_name": emp.get("site_name") or "",
                    "external_id": emp.get("external_id"),
                    "created_at": now,
                    "created_by": "sync",
                }
                users.append(new_user)
                by_username[uname] = new_user
                added.append({"username": uname, "user_id": uid, "role": new_user["role"], "site_name": new_user["site_name"]})
            else:
                if not existing.get("is_active", True):
                    existing["is_active"] = True

                changed = False
                new_dn = emp.get("display_name") or uname
                new_site = emp.get("site_name") or existing.get("site_name") or ""
                new_gender = emp.get("gender") or existing.get("gender") or ""
                new_pos = emp.get("position") or existing.get("position") or ""
                new_ext = emp.get("external_id")

                # ✅ OJO: NO PISAR ROLE si está bloqueado (promovido manualmente)
                incoming_role = _normalize_role(emp.get("role") or existing.get("role") or "EMPLEADO") or "EMPLEADO"

                if (existing.get("display_name") or "") != new_dn:
                    existing["display_name"] = new_dn
                    changed = True

                if (existing.get("site_name") or "") != new_site:
                    existing["site_name"] = new_site
                    changed = True

                if (existing.get("gender") or "") != new_gender:
                    existing["gender"] = new_gender
                    changed = True

                if (existing.get("position") or "") != new_pos:
                    existing["position"] = new_pos
                    changed = True

                if existing.get("dni") != (emp.get("dni") or uname):
                    existing["dni"] = emp.get("dni") or uname
                    changed = True

                if existing.get("external_id") != new_ext:
                    existing["external_id"] = new_ext
                    changed = True

                if not existing.get("role_locked", False):
                    if _normalize_role(existing.get("role") or "") != incoming_role:
                        existing["role"] = incoming_role
                        changed = True

                # password: si firma cambia -> recalcular desde cédula
                if (existing.get("password_sig") or "") != pw_sig:
                    salt, pw_hash = _pbkdf2_hash_password(password_plain)
                    existing["salt"] = salt
                    existing["password_hash"] = pw_hash
                    existing["password_sig"] = pw_sig
                    pw_updated_count += 1
                    changed = True

                if changed:
                    updated.append({"username": uname, "user_id": existing.get("id"), "role": existing.get("role"), "site_name": existing.get("site_name")})

        removed_count = 0
        sessions = db.get("sessions") or {}

        for u in users:
            if _is_admin(u):
                continue
            uname = (u.get("username") or "").strip()
            if uname and uname not in incoming_usernames:
                if u.get("is_active", True):
                    u["is_active"] = False
                    removed_count += 1
                    to_del = [sid for sid, s in sessions.items() if s.get("user_id") == u.get("id")]
                    for sid in to_del:
                        sessions.pop(sid, None)

        db["users"] = users
        db["sessions"] = sessions

        if payload_hash != old_hash:
            _audit(db, "users_synced", "system", {
                "added_count": len(added),
                "updated_count": len(updated),
                "removed_count": removed_count,
                "password_updates": pw_updated_count,
                "added_preview": added[:50],
                "updated_preview": updated[:50],
                "total_payload": len(employees),
            })

        db["employers_last_sync_ts"] = now
        db["employers_last_hash"] = payload_hash
        _write_json_file_nolock(DB_FILE, db)

        return {
            "status": "synced",
            "changed": (payload_hash != old_hash),
            "added": len(added),
            "updated": len(updated),
            "removed": removed_count,
            "password_updates": pw_updated_count,
            "total_payload": len(employees),
        }


# ============================================================
# Thumbnails
# ============================================================
def generate_thumbnail(video_path: str, thumb_path: str):
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            return
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames // 2 if total_frames > 0 else 0)
        ret, frame = cap.read()
        if ret and frame is not None:
            cv2.imwrite(thumb_path, frame)
    finally:
        cap.release()


# ============================================================
# Auth: sesiones con cookie
# ============================================================
def _login_user(db: Dict[str, Any], user_id: str) -> str:
    sid = secrets.token_urlsafe(32)
    db.setdefault("sessions", {})
    db["sessions"][sid] = {"user_id": user_id, "created_at": _now_ts(), "last_seen": _now_ts()}
    return sid


def _logout_sid(db: Dict[str, Any], sid: str):
    if sid and sid in db.get("sessions", {}):
        db["sessions"].pop(sid, None)


def _get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        _bootstrap_admin_if_needed(db)

        sessions = db.get("sessions") or {}
        s = sessions.get(sid)
        if not s:
            _write_json_file_nolock(DB_FILE, db)
            return None

        user_id = s.get("user_id")
        user = _find_user(db, user_id)
        if not user or not user.get("is_active", True):
            sessions.pop(sid, None)
            db["sessions"] = sessions
            _write_json_file_nolock(DB_FILE, db)
            return None

        s["last_seen"] = _now_ts()
        sessions[sid] = s
        db["sessions"] = sessions
        _write_json_file_nolock(DB_FILE, db)
        return user


def require_user(request: Request) -> Dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Solo admin")
    return user


# ============================================================
# ✅ Admin: promover/degradar usuario (NUEVO)
# - set_role: cambia role a ADMIN/MANAGER/EMPLEADO y opcionalmente bloquea el rol
# - make_admin: atajo para ADMIN
# ============================================================
@router.post("/admin/users/set_role")
async def admin_set_user_role(data: dict = Body(...), request: Request = None):
    actor = require_admin(request)

    user_id = (data.get("user_id") or "").strip()
    username = (data.get("username") or "").strip()
    new_role = _normalize_role(data.get("role") or "")
    lock_role = bool(data.get("lock_role", True))

    if new_role not in ("ADMIN", "MANAGER", "EMPLEADO"):
        raise HTTPException(status_code=400, detail="role inválido (ADMIN/MANAGER/EMPLEADO)")

    if not user_id and not username:
        raise HTTPException(status_code=400, detail="user_id o username requerido")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        _bootstrap_admin_if_needed(db)

        u = _find_user(db, user_id) if user_id else _find_user_by_username(db, username)
        if not u:
            raise HTTPException(status_code=404, detail="Usuario no existe")

        # no tocar el actor si no quieres, pero se permite (por si necesitas)
        old_role = _normalize_role(u.get("role") or "EMPLEADO")
        was_admin = _is_admin(u)

        # si vas a degradar y es el último admin, bloquear
        if was_admin and new_role not in ("ADMIN", "MANAGER"):
            if _count_admins(db) <= 1:
                raise HTTPException(status_code=400, detail="No puedes degradar al último ADMIN/MANAGER activo")

        u["role"] = new_role
        u["position"] = u.get("position") or new_role
        u["role_locked"] = lock_role  # ✅ clave: SYNC no lo pisa si True

        # si lo haces admin, asegúrate activo
        if new_role in ("ADMIN", "MANAGER"):
            u["is_active"] = True

        # opcional: invalidar sesiones si cambia permisos (recomendado)
        sessions = db.get("sessions") or {}
        to_del = [sid for sid, s in sessions.items() if s.get("user_id") == u.get("id")]
        for sid in to_del:
            sessions.pop(sid, None)
        db["sessions"] = sessions

        _audit(db, "admin_user_role_changed", actor.get("id", "unknown"), {
            "target_user_id": u.get("id"),
            "target_username": u.get("username"),
            "old_role": old_role,
            "new_role": new_role,
            "role_locked": lock_role,
            "sessions_invalidated": len(to_del),
        })

        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({
        "status": "ok",
        "user": {
            "id": u.get("id"),
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "role": u.get("role"),
            "role_locked": u.get("role_locked", False),
            "is_active": u.get("is_active", True),
        }
    })


@router.post("/admin/users/make_admin")
async def admin_make_admin(data: dict = Body(...), request: Request = None):
    # Atajo: role=ADMIN
    data = dict(data or {})
    data["role"] = "ADMIN"
    if "lock_role" not in data:
        data["lock_role"] = True
    return await admin_set_user_role(data=data, request=request)


@router.post("/admin/users/unlock_role")
async def admin_unlock_role(data: dict = Body(...), request: Request = None):
    actor = require_admin(request)
    user_id = (data.get("user_id") or "").strip()
    username = (data.get("username") or "").strip()
    if not user_id and not username:
        raise HTTPException(status_code=400, detail="user_id o username requerido")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        _bootstrap_admin_if_needed(db)

        u = _find_user(db, user_id) if user_id else _find_user_by_username(db, username)
        if not u:
            raise HTTPException(status_code=404, detail="Usuario no existe")

        u["role_locked"] = False
        _audit(db, "admin_user_role_unlocked", actor.get("id", "unknown"), {
            "target_user_id": u.get("id"),
            "target_username": u.get("username"),
        })
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok"})


# ============================================================
# Adjuntos (escuelas/cursos/clases)
# ============================================================
def _safe_ext(filename: str) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower().strip()


def _save_attachment(upload: UploadFile, actor_id: str) -> Dict[str, Any]:
    ext = _safe_ext(upload.filename or "")
    if ext not in ALLOWED_ATTACH_EXT:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: .{ext}")

    attach_id = uuid.uuid4().hex[:10]
    stored_name = f"{attach_id}.{ext}"
    path = ATTACH_DIR / stored_name

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
                except Exception:
                    pass
                try:
                    os.remove(path)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail=f"Adjunto supera {MAX_ATTACH_MB}MB")
            out.write(chunk)

    return {
        "id": attach_id,
        "filename": stored_name,
        "original_name": upload.filename,
        "url": f"/videos/assets/{stored_name}",
        "size": total,
        "content_type": upload.content_type or "",
        "uploaded_at": _now_ts(),
        "uploaded_by": actor_id,
    }


@router.get("/assets/{filename}")
async def get_attachment(filename: str, request: Request):
    _ = require_user(request)

    name = (filename or "").replace("\\", "/").split("/")[-1]
    path = (ATTACH_DIR / name).resolve()
    if not str(path).startswith(str(ATTACH_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Nombre inválido")

    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no existe")

    return FileResponse(str(path), filename=name)


def _attach_to_entity(db: Dict[str, Any], entity: Dict[str, Any], attach_meta: Dict[str, Any]):
    _ensure_entity_defaults(entity)
    entity["attachments"].insert(0, attach_meta)


def _delete_entity_attachment(entity: Dict[str, Any], attachment_id: str) -> Optional[Dict[str, Any]]:
    _ensure_entity_defaults(entity)
    items = entity.get("attachments") or []
    kept = []
    deleted = None
    for a in items:
        if a.get("id") == attachment_id:
            deleted = a
        else:
            kept.append(a)
    entity["attachments"] = kept
    return deleted


# ============================================================
# Comments (clases)
# ============================================================
def _add_comment(db: Dict[str, Any], video_id: str, user_id: str, text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Comentario vacío")
    if len(text) > 1500:
        raise HTTPException(status_code=400, detail="Comentario muy largo")

    c = {
        "id": uuid.uuid4().hex[:10],
        "video_id": video_id,
        "user_id": user_id,
        "text": text,
        "created_at": _now_ts(),
    }
    db.setdefault("comments", [])
    db["comments"].append(c)
    return c


def _list_comments(db: Dict[str, Any], video_id: str) -> List[Dict[str, Any]]:
    out = [c for c in (db.get("comments") or []) if c.get("video_id") == video_id]
    out.sort(key=lambda x: float(x.get("created_at", 0) or 0))
    return out


# ============================================================
# Páginas Auth
# ============================================================
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    sync_users_if_stale(force=False)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    sync_users_if_stale(force=False)

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        _bootstrap_admin_if_needed(db)

        user = _find_user_by_username(db, username)
        if not user or not user.get("is_active", True):
            _write_json_file_nolock(DB_FILE, db)
            return templates.TemplateResponse("login.html", {"request": request, "error": "Usuario o contraseña inválidos"}, status_code=401)

        if not _verify_password(password, user.get("salt", ""), user.get("password_hash", "")):
            _write_json_file_nolock(DB_FILE, db)
            return templates.TemplateResponse("login.html", {"request": request, "error": "Usuario o contraseña inválidos"}, status_code=401)

        sid = _login_user(db, user["id"])
        _audit(db, "user_login", user["id"], {"username": user.get("username")})
        _write_json_file_nolock(DB_FILE, db)

    resp = RedirectResponse(url="/videos/dashboard", status_code=302)
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@router.get("/logout")
async def logout_action(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)
        if sid:
            _logout_sid(db, sid)
        _write_json_file_nolock(DB_FILE, db)

    resp = RedirectResponse(
        url="https://gestion.salchimonster.com/formacion",
        status_code=302
    )
    # Si tu cookie se setea con path/domain específicos, ponlos igual aquí
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp



# ============================================================
# Dashboard
# ============================================================
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    sync_users_if_stale(force=False)

    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/videos/login", status_code=302)

    db = load_db()
    courses = _all_courses_sorted(db)

    if _is_admin(user):
        visible_courses = courses
    else:
        enrolled = {e.get("course_id") for e in db.get("enrollments") if e.get("user_id") == user["id"]}
        visible_courses = [c for c in courses if c.get("id") in enrolled]

    videos = db.get("videos") or []
    prog = db.get("watch_progress") or {}

    course_cards = []
    for c in visible_courses:
        cid = c.get("id")
        course_videos = [v for v in videos if (v.get("project_id") or DEFAULT_COURSE_ID) == cid]
        total = len(course_videos)
        seen = 0
        last_seen_ts = 0

        for v in course_videos:
            key = f"{user['id']}|{v.get('id')}"
            info = prog.get(key)
            if info:
                seen += 1
                last_seen_ts = max(last_seen_ts, float(info.get("last_seen_ts", 0) or 0))

        school = _find_school(db, c.get("school_id") or DEFAULT_SCHOOL_ID) or {"id": DEFAULT_SCHOOL_ID, "name": "General"}

        course_cards.append({
            "course": c,
            "school": school,
            "total_videos": total,
            "seen_videos": seen,
            "last_seen_ts": last_seen_ts,
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "courses": course_cards
    })


# ============================================================
# Admin Manager
# ============================================================
def _build_enrolled_users_payload(
    db: Dict[str, Any],
    course_id: str,
    role: Optional[str] = None,
    site_name: Optional[str] = None,
) -> Dict[str, Any]:
    role_key = _normalize_role(role) if role else ""
    site_key = _normalize_site(site_name) if site_name else ""

    enrolls = [e for e in (db.get("enrollments") or []) if e.get("course_id") == course_id]
    by_uid = {e.get("user_id"): e for e in enrolls if e.get("user_id")}

    users = []
    for uid, e in by_uid.items():
        u = _find_user(db, uid)
        if not u or not u.get("is_active", True):
            continue

        if role_key and _normalize_role(u.get("role") or "") != role_key:
            continue
        if site_key and _normalize_site(u.get("site_name") or "") != site_key:
            continue

        users.append({
            "id": u.get("id"),
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "role": u.get("role"),
            "site_name": u.get("site_name"),
            "position": u.get("position"),
            "dni": u.get("dni"),
            "enrollment_id": e.get("id"),
            "enrolled_at": e.get("enrolled_at"),
            "mode": e.get("mode"),
        })

    users.sort(key=lambda x: (str(x.get("site_name") or ""), str(x.get("role") or ""), str(x.get("display_name") or "")))

    role_groups: Dict[str, int] = {}
    site_groups: Dict[str, int] = {}
    for u in users:
        rk = _normalize_role(u.get("role") or "")
        sk = (u.get("site_name") or "").strip()
        role_groups[rk] = role_groups.get(rk, 0) + 1
        site_groups[sk] = site_groups.get(sk, 0) + 1

    return {
        "course_id": course_id,
        "filters": {"role": role or "", "site_name": site_name or ""},
        "groups": {
            "roles": [{"key": k, "count": role_groups[k]} for k in sorted(role_groups.keys()) if k],
            "sites": [{"key": k, "count": site_groups[k]} for k in sorted(site_groups.keys()) if k],
        },
        "users": users,
        "count": len(users),
    }


@router.get("/manager", response_class=HTMLResponse)
async def manager(
    request: Request,
    school_id: Optional[str] = Query(default=None),
    course_id: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    site_name: Optional[str] = Query(default=None),
):
    sync_users_if_stale(force=False)

    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/videos/login", status_code=302)
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Solo admin")

    db = load_db()

    schools = _all_schools_sorted(db)

    school_id = (school_id or "").strip() or DEFAULT_SCHOOL_ID
    selected_school = _find_school(db, school_id)
    if not selected_school:
        selected_school = _find_school(db, DEFAULT_SCHOOL_ID)
        school_id = DEFAULT_SCHOOL_ID

    courses = _courses_for_school(db, school_id)

    course_id = (course_id or "").strip()
    selected_course = None
    if course_id:
        found = next((c for c in courses if c["id"] == course_id), None)
        if found:
            selected_course = found
    if not selected_course and courses:
        selected_course = courses[0]

    selected_course_id = (selected_course.get("id") if selected_course else "")

    videos = []
    if selected_course_id:
        all_videos = db.get("videos") or []
        videos = [v for v in all_videos if (v.get("project_id") or DEFAULT_COURSE_ID) == selected_course_id]
        videos.sort(key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True)

    enrolled_payload = _build_enrolled_users_payload(db, selected_course_id, role=role, site_name=site_name) if selected_course_id else {
        "course_id": "",
        "filters": {"role": role or "", "site_name": site_name or ""},
        "groups": {"roles": [], "sites": []},
        "users": [],
        "count": 0
    }

    file_colors = load_colors()

    return templates.TemplateResponse("manager.html", {
        "request": request,
        "user": user,
        "schools": schools,
        "selected_school": selected_school,
        "courses": courses,
        "selected_course": selected_course,
        "videos": videos,
        "enrolled": enrolled_payload,
        "colors": file_colors
    })


@router.get("/admin/enrollments")
async def admin_enrollments(
    request: Request,
    course_id: str = Query(...),
    role: Optional[str] = Query(default=None),
    site_name: Optional[str] = Query(default=None),
):
    _ = require_admin(request)
    db = load_db()
    course_id = (course_id or "").strip()
    if not _find_course(db, course_id):
        raise HTTPException(status_code=404, detail="Curso no existe")
    return JSONResponse(_build_enrolled_users_payload(db, course_id, role=role, site_name=site_name))


# ============================================================
# ✅ MATRÍCULAS (ENROLL)
# ============================================================
def _add_enrollment_if_missing(db: Dict[str, Any], course_id: str, user_id: str, actor_id: str, mode: str) -> bool:
    """Return True si agregó; False si ya existía."""
    if _is_enrolled(db, user_id, course_id):
        return False
    db.setdefault("enrollments", [])
    db["enrollments"].append({
        "id": uuid.uuid4().hex[:10],
        "course_id": course_id,
        "user_id": user_id,
        "enrolled_at": _now_ts(),
        "enrolled_by": actor_id,
        "mode": mode,
    })
    return True


@router.post("/courses/enroll")
async def enroll_user(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    user_id = (data.get("user_id") or "").strip()
    if not course_id or not user_id:
        raise HTTPException(status_code=400, detail="course_id y user_id requeridos")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso no existe")

        u = _find_user(db, user_id)
        if not u or not u.get("is_active", True):
            raise HTTPException(status_code=404, detail="Usuario no existe o inactivo")

        if _is_admin(u):
            raise HTTPException(status_code=400, detail="No se matricula ADMIN/MANAGER")

        added = _add_enrollment_if_missing(db, course_id, user_id, actor["id"], mode="individual")
        _audit(db, "user_enrolled", actor["id"], {"course_id": course_id, "user_id": user_id, "added": added})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "added": bool(added)})


@router.post("/courses/enroll_by_site")
async def enroll_by_site(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    site_name = (data.get("site_name") or "").strip()
    if not course_id or not site_name:
        raise HTTPException(status_code=400, detail="course_id y site_name requeridos")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso no existe")

        targets = _filter_employees(db, site_name=site_name, role=None)
        added_count = 0
        for u in targets:
            if _add_enrollment_if_missing(db, course_id, u["id"], actor["id"], mode="site"):
                added_count += 1

        _audit(db, "users_enrolled_by_site", actor["id"], {
            "course_id": course_id,
            "site_name": site_name,
            "targets": len(targets),
            "added": added_count,
        })
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "targets": len(targets), "added": added_count})


@router.post("/courses/enroll_by_role")
async def enroll_by_role(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    role = (data.get("role") or "").strip()
    if not course_id or not role:
        raise HTTPException(status_code=400, detail="course_id y role requeridos")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso no existe")

        targets = _filter_employees(db, site_name=None, role=role)
        added_count = 0
        for u in targets:
            if _add_enrollment_if_missing(db, course_id, u["id"], actor["id"], mode="role"):
                added_count += 1

        _audit(db, "users_enrolled_by_role", actor["id"], {
            "course_id": course_id,
            "role": _normalize_role(role),
            "targets": len(targets),
            "added": added_count,
        })
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "targets": len(targets), "added": added_count})


@router.post("/courses/enroll_by_site_role")
async def enroll_by_site_role(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    site_name = (data.get("site_name") or "").strip()
    role = (data.get("role") or "").strip()
    if not course_id or not site_name or not role:
        raise HTTPException(status_code=400, detail="course_id, site_name y role requeridos")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso no existe")

        targets = _filter_employees(db, site_name=site_name, role=role)
        added_count = 0
        for u in targets:
            if _add_enrollment_if_missing(db, course_id, u["id"], actor["id"], mode="site_role"):
                added_count += 1

        _audit(db, "users_enrolled_by_site_role", actor["id"], {
            "course_id": course_id,
            "site_name": site_name,
            "role": _normalize_role(role),
            "targets": len(targets),
            "added": added_count,
        })
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "targets": len(targets), "added": added_count})


# ============================================================
# Unenroll
# ============================================================
@router.post("/courses/unenroll")
async def unenroll_user(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    user_id = (data.get("user_id") or "").strip()
    if not course_id or not user_id:
        raise HTTPException(status_code=400, detail="course_id y user_id requeridos")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        before = len(db.get("enrollments") or [])
        db["enrollments"] = [e for e in (db.get("enrollments") or []) if not (e.get("course_id") == course_id and e.get("user_id") == user_id)]
        after = len(db.get("enrollments") or [])

        _audit(db, "user_unenrolled", actor["id"], {"course_id": course_id, "user_id": user_id, "removed": (before - after)})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok"})


@router.post("/courses/unenroll_bulk")
async def unenroll_bulk(request: Request, data: dict = Body(...)):
    """
    Desmatricular en masa.
    - Si se dan user_ids, borra esos.
    - Si NO se dan user_ids, usa filtros (role/site).
    - Si NO hay user_ids Y NO hay filtros -> Borra TODOS los del curso.
    """
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    user_ids = data.get("user_ids") or []
    role = (data.get("role") or "").strip()
    site_name = (data.get("site_name") or "").strip()

    if not course_id:
        raise HTTPException(status_code=400, detail="course_id requerido")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso no existe")

        targets = set(str(x) for x in user_ids if str(x).strip())

        if not targets:
            enrolled_payload = _build_enrolled_users_payload(
                db,
                course_id,
                role=(role if role else None),
                site_name=(site_name if site_name else None)
            )
            targets = set(u["id"] for u in enrolled_payload["users"])

        if not targets:
            return JSONResponse({"status": "ok", "removed": 0, "message": "Nada que borrar"})

        before = len(db.get("enrollments") or [])

        db["enrollments"] = [
            e for e in (db.get("enrollments") or [])
            if not (e.get("course_id") == course_id and str(e.get("user_id") or "") in targets)
        ]

        after = len(db.get("enrollments") or [])
        removed = before - after

        _audit(db, "users_unenrolled_bulk", actor["id"], {
            "course_id": course_id,
            "targets_count": len(targets),
            "removed": removed,
            "by_filters": {"role": role, "site_name": site_name},
        })

        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "removed": removed})


# ============================================================
# WATCH (playlist + prev/next + adjuntos + comentarios)
# ============================================================
def _mark_watch(db: Dict[str, Any], user_id: str, video_id: str):
    today = _today_bogota_str()
    key = f"{user_id}|{video_id}"
    prog = db.get("watch_progress") or {}
    info = prog.get(key) or {"last_seen_ts": 0, "seen_days": {}}

    seen_days = info.get("seen_days") or {}
    if today not in seen_days:
        for v in db.get("videos", []):
            if v.get("id") == video_id:
                v["views"] = int(v.get("views", 0) or 0) + 1
                break

    seen_days[today] = _now_ts()
    info["seen_days"] = seen_days
    info["last_seen_ts"] = _now_ts()

    prog[key] = info
    db["watch_progress"] = prog


@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    sync_users_if_stale(force=False)

    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/videos/login", status_code=302)

    db = load_db()
    video = next((v for v in (db.get("videos", []) or []) if v.get("id") == video_id), None)
    if not video:
        return HTMLResponse("<h1>Video no encontrado</h1>", status_code=404)

    course_id = (video.get("project_id") or DEFAULT_COURSE_ID)
    course = _find_course(db, course_id)
    if not course:
        return HTMLResponse("<h1>Curso no encontrado</h1>", status_code=404)

    if not _is_admin(user):
        if not _is_enrolled(db, user["id"], course_id):
            raise HTTPException(status_code=403, detail="No estás matriculado en este curso")

    playlist = [v for v in (db.get("videos") or []) if (v.get("project_id") or DEFAULT_COURSE_ID) == course_id]
    playlist.sort(key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True)

    idx = 0
    for i, v in enumerate(playlist):
        if v.get("id") == video_id:
            idx = i
            break

    prev_video = playlist[idx - 1] if idx - 1 >= 0 else None
    next_video = playlist[idx + 1] if idx + 1 < len(playlist) else None

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db2 = _ensure_db_schema(raw)
        _mark_watch(db2, user["id"], video_id)
        _audit(db2, "video_watched", user["id"], {"video_id": video_id, "course_id": course_id})
        _write_json_file_nolock(DB_FILE, db2)

    db = load_db()
    video = next((v for v in (db.get("videos", []) or []) if v.get("id") == video_id), video)

    comments = _list_comments(db, video_id)
    by_id = {u.get("id"): u for u in (db.get("users") or [])}
    for c in comments:
        u = by_id.get(c.get("user_id"))
        c["_author"] = (u.get("display_name") if u else c.get("user_id"))

    file_colors = load_colors()

    return templates.TemplateResponse("watch.html", {
        "request": request,
        "user": user,
        "course": course,
        "video": video,
        "playlist": playlist,
        "prev_video": prev_video,
        "next_video": next_video,
        "comments": comments,
        "colors": file_colors
    })


@router.post("/videos/{video_id}/comments")
async def add_video_comment(video_id: str, request: Request, text: str = Form(...)):
    user = require_user(request)
    db = load_db()

    video = next((v for v in (db.get("videos") or []) if v.get("id") == video_id), None)
    if not video:
        raise HTTPException(status_code=404, detail="Video no existe")

    course_id = (video.get("project_id") or DEFAULT_COURSE_ID)
    if not _is_admin(user):
        if not _is_enrolled(db, user["id"], course_id):
            raise HTTPException(status_code=403, detail="No estás matriculado")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db2 = _ensure_db_schema(raw)
        c = _add_comment(db2, video_id, user["id"], text)
        _audit(db2, "comment_added", user["id"], {"video_id": video_id, "comment_id": c["id"]})
        _write_json_file_nolock(DB_FILE, db2)

    return RedirectResponse(url=f"/videos/watch/{video_id}", status_code=303)


@router.get("/course/{course_id}", response_class=HTMLResponse)
async def course_page(request: Request, course_id: str):
    sync_users_if_stale(force=False)

    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/videos/login", status_code=302)

    db = load_db()
    course = _find_course(db, course_id)
    if not course:
        return HTMLResponse("<h1>Curso no encontrado</h1>", status_code=404)

    if not _is_admin(user):
        if not _is_enrolled(db, user["id"], course_id):
            raise HTTPException(status_code=403, detail="No estás matriculado en este curso")

    videos = [v for v in (db.get("videos") or []) if (v.get("project_id") or DEFAULT_COURSE_ID) == course_id]
    videos.sort(key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True)

    prog = db.get("watch_progress") or {}
    for v in videos:
        key = f"{user['id']}|{v.get('id')}"
        v["_seen"] = True if prog.get(key) else False

    school = _find_school(db, course.get("school_id") or DEFAULT_SCHOOL_ID) or {"id": DEFAULT_SCHOOL_ID, "name": "General"}
    file_colors = load_colors()

    return templates.TemplateResponse("course.html", {
        "request": request,
        "user": user,
        "course": course,
        "school": school,
        "videos": videos,
        "colors": file_colors
    })


# ============================================================
# SCHOOLS / COURSES: create + update + attachments
# ============================================================
@router.post("/schools/create")
async def create_school(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")

    sid = uuid.uuid4().hex[:8]

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        for s in db.get("schools", []):
            if (s.get("name") or "").strip().lower() == name.lower():
                raise HTTPException(status_code=400, detail="Ya existe una escuela con ese nombre")

        school = {
            "id": sid,
            "name": name,
            "description": description,
            "attachments": [],
            "created_at": _now_ts(),
            "created_by": actor["id"],
        }
        db["schools"].append(school)
        _audit(db, "school_created", actor["id"], {"school_id": sid, "name": name})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "school": {"id": sid, "name": name}})


@router.post("/schools/update")
async def update_school(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    school_id = (data.get("school_id") or "").strip()
    if not school_id:
        raise HTTPException(status_code=400, detail="school_id requerido")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        s = _find_school(db, school_id)
        if not s:
            raise HTTPException(status_code=404, detail="Escuela no existe")

        if "name" in data:
            s["name"] = (data.get("name") or "").strip() or s.get("name")
        if "description" in data:
            s["description"] = (data.get("description") or "").strip()

        _audit(db, "school_updated", actor["id"], {"school_id": school_id})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok"})


@router.post("/schools/{school_id}/attachments")
async def upload_school_attachment(school_id: str, request: Request, file: UploadFile = File(...)):
    actor = require_admin(request)
    school_id = (school_id or "").strip()

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        s = _find_school(db, school_id)
        if not s:
            raise HTTPException(status_code=404, detail="Escuela no existe")

        meta = _save_attachment(file, actor["id"])
        _attach_to_entity(db, s, meta)
        _audit(db, "school_attachment_added", actor["id"], {"school_id": school_id, "attachment_id": meta["id"]})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "attachment": meta})


@router.post("/courses/create")
async def create_course(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    name = (data.get("name") or "").strip()
    school_id = (data.get("school_id") or DEFAULT_SCHOOL_ID).strip() or DEFAULT_SCHOOL_ID
    description = (data.get("description") or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")

    course_id = uuid.uuid4().hex[:8]

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_school(db, school_id):
            raise HTTPException(status_code=404, detail="Escuela no existe")

        for c in db.get("projects", []):
            if (c.get("name") or "").strip().lower() == name.lower() and (c.get("school_id") or DEFAULT_SCHOOL_ID) == school_id:
                raise HTTPException(status_code=400, detail="Ya existe un curso con ese nombre en esa escuela")

        course = {
            "id": course_id,
            "name": name,
            "school_id": school_id,
            "description": description,
            "attachments": [],
            "created_at": _now_ts(),
            "created_by": actor["id"],
        }
        db["projects"].append(course)
        _audit(db, "course_created", actor["id"], {"course_id": course_id, "name": name, "school_id": school_id})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "course": {"id": course_id, "name": name, "school_id": school_id}})


@router.post("/courses/update")
async def update_course(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id requerido")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        c = _find_course(db, course_id)
        if not c:
            raise HTTPException(status_code=404, detail="Curso no existe")

        if "name" in data:
            c["name"] = (data.get("name") or "").strip() or c.get("name")
        if "description" in data:
            c["description"] = (data.get("description") or "").strip()
        if "school_id" in data:
            sid = (data.get("school_id") or "").strip()
            if sid and _find_school(db, sid):
                c["school_id"] = sid

        _audit(db, "course_updated", actor["id"], {"course_id": course_id})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok"})


@router.post("/courses/{course_id}/attachments")
async def upload_course_attachment(course_id: str, request: Request, file: UploadFile = File(...)):
    actor = require_admin(request)
    course_id = (course_id or "").strip()

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        c = _find_course(db, course_id)
        if not c:
            raise HTTPException(status_code=404, detail="Curso no existe")

        meta = _save_attachment(file, actor["id"])
        _attach_to_entity(db, c, meta)
        _audit(db, "course_attachment_added", actor["id"], {"course_id": course_id, "attachment_id": meta["id"]})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "attachment": meta})


@router.post("/attachments/delete")
async def delete_attachment(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    etype = (data.get("entity_type") or "").strip().lower()
    entity_id = (data.get("entity_id") or "").strip()
    attachment_id = (data.get("attachment_id") or "").strip()
    if etype not in ("school", "course", "video"):
        raise HTTPException(status_code=400, detail="entity_type inválido")
    if not entity_id or not attachment_id:
        raise HTTPException(status_code=400, detail="entity_id y attachment_id requeridos")

    deleted_meta = None

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if etype == "school":
            ent = _find_school(db, entity_id)
        elif etype == "course":
            ent = _find_course(db, entity_id)
        else:
            ent = next((v for v in (db.get("videos") or []) if v.get("id") == entity_id), None)

        if not ent:
            raise HTTPException(status_code=404, detail="Entidad no existe")

        deleted_meta = _delete_entity_attachment(ent, attachment_id)
        if not deleted_meta:
            raise HTTPException(status_code=404, detail="Adjunto no existe")

        _audit(db, "attachment_deleted", actor["id"], {
            "entity_type": etype,
            "entity_id": entity_id,
            "attachment_id": attachment_id,
        })

        _write_json_file_nolock(DB_FILE, db)

    try:
        fn = (deleted_meta.get("filename") or "").split("/")[-1]
        if fn:
            os.remove(ATTACH_DIR / fn)
    except Exception:
        pass

    return JSONResponse({"status": "ok"})


# ============================================================
# Cursos del usuario
# ============================================================
@router.get("/courses/my")
async def my_courses(request: Request):
    user = require_user(request)
    db = load_db()
    courses = _all_courses_sorted(db)

    if _is_admin(user):
        return JSONResponse({"courses": courses})

    enrolled = {e.get("course_id") for e in db.get("enrollments") if e.get("user_id") == user["id"]}
    visible = [c for c in courses if c.get("id") in enrolled]
    return JSONResponse({"courses": visible})


# ============================================================
# Admin: sync + users
# ============================================================
@router.post("/admin/sync_users")
async def admin_sync_users(request: Request):
    _ = require_admin(request)
    sync = sync_users_if_stale(force=True)
    return JSONResponse({"sync": sync})


@router.get("/admin/users")
async def admin_users(request: Request):
    _ = require_admin(request)
    db = load_db()
    users = db.get("users") or []

    safe = []
    for u in users:
        safe.append({
            "id": u.get("id"),
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "role": u.get("role"),
            "role_locked": bool(u.get("role_locked", False)),
            "is_active": u.get("is_active", True),
            "dni": u.get("dni"),
            "gender": u.get("gender"),
            "position": u.get("position"),
            "site_name": u.get("site_name"),
            "external_id": u.get("external_id"),
            "created_at": u.get("created_at"),
            "created_by": u.get("created_by"),
        })
    return JSONResponse({"users": safe})


# ============================================================
# Upload / Update / Delete videos (solo admin)
# ============================================================
@router.post("/upload")
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
):
    actor = require_admin(request)

    unique_hash = uuid.uuid4().hex[:5]
    if not title:
        title = unique_hash

    extension = (file.filename.split(".")[-1] if file.filename and "." in file.filename else "mp4").lower()
    new_filename = f"{unique_hash}.{extension}"
    thumb_filename = f"{unique_hash}.jpg"

    video_path = UPLOAD_DIR / new_filename
    thumb_path = THUMB_DIR / thumb_filename

    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        generate_thumbnail(str(video_path), str(thumb_path))
    except Exception:
        pass

    course_id = (project_id or DEFAULT_COURSE_ID).strip() or DEFAULT_COURSE_ID

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            course_id = DEFAULT_COURSE_ID

        new_entry = {
            "id": unique_hash,
            "title": title,
            "description": (description or "").strip(),
            "attachments": [],
            "filename": new_filename,
            "thumb": thumb_filename,
            "twitter_link": "",
            "original_name": file.filename,
            "views": 0,
            "timestamp": os.path.getmtime(video_path),
            "project_id": course_id,
            "created_at": _now_ts(),
            "created_by": actor["id"],
        }

        db["videos"].insert(0, new_entry)
        _audit(db, "video_uploaded", actor["id"], {"video_id": unique_hash, "course_id": course_id, "title": title})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"message": "Subido con éxito", "video": new_entry})


@router.post("/videos/{video_id}/update")
async def update_video(video_id: str, request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    video_id = (video_id or "").strip()

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        v = next((x for x in (db.get("videos") or []) if x.get("id") == video_id), None)
        if not v:
            raise HTTPException(status_code=404, detail="Video no existe")

        if "title" in data:
            v["title"] = (data.get("title") or "").strip() or v.get("title")
        if "description" in data:
            v["description"] = (data.get("description") or "").strip()
        if "project_id" in data:
            cid = (data.get("project_id") or "").strip()
            if cid and _find_course(db, cid):
                v["project_id"] = cid

        _audit(db, "video_updated", actor["id"], {"video_id": video_id})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok"})


@router.post("/videos/{video_id}/attachments")
async def upload_video_attachment(video_id: str, request: Request, file: UploadFile = File(...)):
    actor = require_admin(request)
    video_id = (video_id or "").strip()

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        v = next((x for x in (db.get("videos") or []) if x.get("id") == video_id), None)
        if not v:
            raise HTTPException(status_code=404, detail="Video no existe")

        meta = _save_attachment(file, actor["id"])
        _attach_to_entity(db, v, meta)
        _audit(db, "video_attachment_added", actor["id"], {"video_id": video_id, "attachment_id": meta["id"]})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "attachment": meta})


@router.post("/move")
async def move_video(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    video_id = (data.get("id") or "").strip()
    course_id = (data.get("project_id") or "").strip()

    if not video_id or not course_id:
        raise HTTPException(status_code=400, detail="id y project_id requeridos")

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso destino no existe")

        found = False
        for v in db.get("videos", []):
            if v.get("id") == video_id:
                v["project_id"] = course_id
                found = True
                break

        if not found:
            raise HTTPException(status_code=404, detail="Video no existe")

        _audit(db, "video_moved", actor["id"], {"video_id": video_id, "course_id": course_id})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok"})


@router.get("/delete/{video_id}")
async def delete_video(video_id: str, request: Request):
    actor = require_admin(request)

    filename_to_del = None
    thumb_to_del = None

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        new_videos = []
        for v in db.get("videos", []):
            if v.get("id") == video_id:
                filename_to_del = v.get("filename")
                thumb_to_del = v.get("thumb")
            else:
                new_videos.append(v)

        db["videos"] = new_videos

        prog = db.get("watch_progress") or {}
        keys = [k for k in prog.keys() if k.endswith(f"|{video_id}")]
        for k in keys:
            prog.pop(k, None)
        db["watch_progress"] = prog

        db["comments"] = [c for c in (db.get("comments") or []) if c.get("video_id") != video_id]

        _audit(db, "video_deleted", actor["id"], {"video_id": video_id})
        _write_json_file_nolock(DB_FILE, db)

    if filename_to_del:
        try:
            os.remove(UPLOAD_DIR / filename_to_del)
        except Exception:
            pass
    if thumb_to_del:
        try:
            os.remove(THUMB_DIR / thumb_to_del)
        except Exception:
            pass

    return JSONResponse({"status": "deleted"})


# ============================================================
# REPORTES
# ============================================================
@router.get("/reports/course/{course_id}")
async def report_course(course_id: str, request: Request):
    _ = require_admin(request)

    db = load_db()
    course = _find_course(db, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Curso no existe")

    course_videos = [v for v in db.get("videos", []) if (v.get("project_id") or DEFAULT_COURSE_ID) == course_id]
    video_ids = [v.get("id") for v in course_videos]
    total_videos = len(video_ids)

    enrolled = [e for e in db.get("enrollments", []) if e.get("course_id") == course_id]
    prog = db.get("watch_progress") or {}

    rows = []
    for e in enrolled:
        u = _find_user(db, e.get("user_id"))
        if not u:
            continue

        seen = 0
        last_seen = 0
        seen_list = []
        for vid in video_ids:
            key = f"{u['id']}|{vid}"
            info = prog.get(key)
            if info:
                seen += 1
                last_seen = max(last_seen, float(info.get("last_seen_ts", 0) or 0))
                seen_list.append({"video_id": vid, "last_seen_ts": info.get("last_seen_ts", 0)})

        rows.append({
            "user_id": u["id"],
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "role": u.get("role"),
            "site_name": u.get("site_name"),
            "seen_videos": seen,
            "total_videos": total_videos,
            "pct": (0 if total_videos == 0 else round(seen * 100 / total_videos, 1)),
            "last_seen_ts": last_seen,
            "details": seen_list
        })

    rows.sort(key=lambda r: (r["seen_videos"], r["last_seen_ts"]))

    return JSONResponse({
        "course": {"id": course_id, "name": course.get("name"), "school_id": course.get("school_id")},
        "total_videos": total_videos,
        "enrolled_count": len(enrolled),
        "rows": rows
    })


@router.get("/reports/video/{video_id}")
async def report_video(video_id: str, request: Request):
    _ = require_admin(request)

    db = load_db()
    video = next((v for v in db.get("videos", []) if v.get("id") == video_id), None)
    if not video:
        raise HTTPException(status_code=404, detail="Video no existe")

    prog = db.get("watch_progress") or {}
    watchers = []
    for k, info in prog.items():
        try:
            uid, vid = k.split("|", 1)
        except Exception:
            continue
        if vid != video_id:
            continue
        u = _find_user(db, uid)
        if not u:
            continue
        watchers.append({
            "user_id": uid,
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "role": u.get("role"),
            "site_name": u.get("site_name"),
            "last_seen_ts": info.get("last_seen_ts", 0),
            "seen_days": list((info.get("seen_days") or {}).keys()),
        })

    watchers.sort(key=lambda x: float(x.get("last_seen_ts", 0) or 0), reverse=True)

    return JSONResponse({
        "video": {"id": video_id, "title": video.get("title"), "course_id": video.get("project_id")},
        "watchers_count": len(watchers),
        "watchers": watchers
    })


# ============================================================
# Courses: borrar curso (solo admin)
# ============================================================
@router.post("/courses/delete")
async def delete_course(request: Request, data: dict = Body(...)):
    actor = require_admin(request)
    course_id = (data.get("course_id") or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id requerido")
    if course_id == DEFAULT_COURSE_ID:
        raise HTTPException(status_code=400, detail="No se puede borrar el curso base")

    files_to_delete = []
    thumbs_to_delete = []
    video_ids_deleted = []

    with FileLock(str(LOCK_FILE)):
        raw = _read_json_file_nolock(DB_FILE, {})
        db = _ensure_db_schema(raw)

        course = _find_course(db, course_id)
        if not course:
            raise HTTPException(status_code=404, detail="Curso no existe")

        db["projects"] = [p for p in db.get("projects", []) if p.get("id") != course_id]

        new_videos = []
        for v in db.get("videos", []):
            if (v.get("project_id") or DEFAULT_COURSE_ID) == course_id:
                video_ids_deleted.append(v.get("id"))
                files_to_delete.append(v.get("filename"))
                thumbs_to_delete.append(v.get("thumb"))
            else:
                new_videos.append(v)
        db["videos"] = new_videos

        db["enrollments"] = [e for e in db.get("enrollments", []) if e.get("course_id") != course_id]

        prog = db.get("watch_progress") or {}
        if video_ids_deleted:
            for k in list(prog.keys()):
                try:
                    _, vid = k.split("|", 1)
                except Exception:
                    continue
                if vid in video_ids_deleted:
                    prog.pop(k, None)
        db["watch_progress"] = prog

        if video_ids_deleted:
            db["comments"] = [c for c in (db.get("comments") or []) if c.get("video_id") not in set(video_ids_deleted)]

        _audit(db, "course_deleted", actor["id"], {"course_id": course_id, "videos_deleted": len(video_ids_deleted)})
        _write_json_file_nolock(DB_FILE, db)

    for fn in files_to_delete:
        if fn:
            try:
                os.remove(UPLOAD_DIR / fn)
            except Exception:
                pass
    for tn in thumbs_to_delete:
        if tn:
            try:
                os.remove(THUMB_DIR / tn)
            except Exception:
                pass

    return JSONResponse({"status": "deleted", "course_id": course_id, "videos_deleted": len(video_ids_deleted)})
