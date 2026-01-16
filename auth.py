"""
Sistema de autenticación y gestión de usuarios.
Basado en video_router.py pero adaptado para proyectos.
"""
import os
import json
import secrets
import hashlib
import time
import uuid
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any, List
from filelock import FileLock

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

# Configuración
USERS_DB_FILE = Path("data/users.json")
USERS_LOCK_FILE = Path("data/users.json.lock")
SESSION_COOKIE = "pm_session"

# Seguridad
BOOTSTRAP_ADMIN_USER = os.getenv("ADMIN_USER", "andrew19f")
BOOTSTRAP_ADMIN_PASS = os.getenv("ADMIN_PASS", "change-me-please")

# Sincronización de usuarios desde backend
EMPLOYERS_URL = os.getenv("EMPLOYERS_URL", "https://backend.salchimonster.com/employers-basic")
EMPLOYERS_SYNC_TTL_SECONDS = int(os.getenv("EMPLOYERS_SYNC_TTL_SECONDS", "3600"))

# Asegurar directorio
USERS_DB_FILE.parent.mkdir(parents=True, exist_ok=True)


def ensure_db_initialized() -> None:
    """Asegura que la base de datos esté inicializada. Si no existe, la crea sincronizando desde employers."""
    # Verificar si existe el archivo de usuarios
    if not USERS_DB_FILE.exists():
        print("Base de datos de usuarios no encontrada. Inicializando desde employers...")
        try:
            # Forzar sincronización inicial
            result = sync_users_if_stale(force=True)
            print(f"Base de datos inicializada. Estado: {result.get('status', 'unknown')}")
            if result.get('status') == 'error':
                print(f"Error durante inicialización: {result.get('error', 'Unknown error')}")
        except Exception as e:
            print(f"Error crítico inicializando base de datos: {e}")
            # Crear estructura mínima para evitar errores
            try:
                with FileLock(str(USERS_LOCK_FILE), timeout=2):
                    db = {"users": [], "sessions": {}}
                    _bootstrap_admin_if_needed(db)
                    _write_users_db(db)
            except Exception:
                pass
    else:
        # Si existe, solo verificar que tenga el admin bootstrap
        try:
            with FileLock(str(USERS_LOCK_FILE), timeout=2):
                db = _read_users_db()
                _bootstrap_admin_if_needed(db)
                _write_users_db(db)
        except Exception:
            pass


def _now_ts() -> float:
    return time.time()


def _read_users_db() -> Dict[str, Any]:
    """Lee la base de datos de usuarios."""
    if not USERS_DB_FILE.exists():
        return {"users": [], "sessions": {}}
    try:
        with open(USERS_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"users": [], "sessions": {}}
            return data
    except Exception:
        return {"users": [], "sessions": {}}


def _write_users_db(db: Dict[str, Any]) -> None:
    """Escribe la base de datos de usuarios."""
    tmp = str(USERS_DB_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, USERS_DB_FILE)


def _pbkdf2_hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    """Hash de contraseña usando PBKDF2."""
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)
    
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, 200_000)
    return salt_hex, dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Verifica una contraseña."""
    _, calc = _pbkdf2_hash_password(password, salt_hex=salt_hex)
    return secrets.compare_digest(calc, hash_hex)


def _normalize_role(role: str) -> str:
    """Normaliza el rol a mayúsculas."""
    return (role or "").strip().upper()


def _is_admin(user: Dict[str, Any]) -> bool:
    """Verifica si un usuario es admin."""
    r = (user or {}).get("role") or ""
    return str(r).strip().upper() in ("ADMIN", "MANAGER")


def _bootstrap_admin_if_needed(db: Dict[str, Any]) -> None:
    """Crea un admin por defecto si no existe ninguno."""
    users = db.get("users", [])
    has_admin = any(_is_admin(u) and u.get("is_active", True) for u in users)
    
    if not has_admin:
        salt, pw_hash = _pbkdf2_hash_password(BOOTSTRAP_ADMIN_PASS)
        admin_id = secrets.token_urlsafe(12)
        users.append({
            "id": admin_id,
            "username": BOOTSTRAP_ADMIN_USER,
            "display_name": "Administrador",
            "email": "",
            "role": "ADMIN",
            "role_locked": True,  # Que SYNC no lo pise nunca
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
            "created_by": "system"
        })
        db["users"] = users


def _find_user_by_username(db: Dict[str, Any], username: str) -> Optional[Dict[str, Any]]:
    """Busca un usuario por username."""
    ukey = (username or "").strip().lower()
    for u in db.get("users", []):
        if (u.get("username") or "").strip().lower() == ukey:
            return u
    return None


def _find_user(db: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    """Busca un usuario por ID."""
    for u in db.get("users", []):
        if u.get("id") == user_id:
            return u
    return None


def _login_user(db: Dict[str, Any], user_id: str) -> str:
    """Crea una sesión para un usuario."""
    sid = secrets.token_urlsafe(32)
    db.setdefault("sessions", {})
    db["sessions"][sid] = {
        "user_id": user_id,
        "created_at": _now_ts(),
        "last_seen": _now_ts()
    }
    return sid


def _logout_sid(db: Dict[str, Any], sid: str) -> None:
    """Elimina una sesión."""
    if sid and sid in db.get("sessions", {}):
        db["sessions"].pop(sid, None)


def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    """Obtiene el usuario actual desde la cookie de sesión."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    
    try:
        with FileLock(str(USERS_LOCK_FILE), timeout=1):
            db = _read_users_db()
            _bootstrap_admin_if_needed(db)
            
            sessions = db.get("sessions", {})
            s = sessions.get(sid)
            if not s:
                return None
            
            user_id = s.get("user_id")
            user = _find_user(db, user_id)
            if not user or not user.get("is_active", True):
                sessions.pop(sid, None)
                db["sessions"] = sessions
                _write_users_db(db)
                return None
            
            # Actualizar last_seen
            s["last_seen"] = _now_ts()
            sessions[sid] = s
            db["sessions"] = sessions
            _write_users_db(db)
            
            return user
    except Exception:
        return None


def require_user(request: Request) -> Dict[str, Any]:
    """Requiere que el usuario esté autenticado."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    """Requiere que el usuario sea admin."""
    user = require_user(request)
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Solo administradores")
    return user


def get_user_avatar_url(user: Dict[str, Any]) -> str:
    """Obtiene la URL del avatar de un usuario.
    
    Si tiene DNI, devuelve la URL de la foto real.
    Si no tiene DNI, devuelve la URL del avatar basado en el género.
    """
    dni = user.get("dni", "").strip()
    if dni:
        # Usar foto real del backend
        timestamp = int(time.time())
        return f"https://backend.salchimonster.com/read-product-image/300/employer-{dni}?timestamp={timestamp}"
    
    # Usar avatar basado en género
    gender = (user.get("gender", "") or "").strip()
    if gender == "Masculino":
        return "/api/avatars/masculino.png"
    elif gender == "Femenino":
        return "/api/avatars/femenino.png"
    else:
        return "/api/avatars/default.png"


def get_all_users() -> List[Dict[str, Any]]:
    """Obtiene todos los usuarios."""
    try:
        with FileLock(str(USERS_LOCK_FILE), timeout=1):
            db = _read_users_db()
            _bootstrap_admin_if_needed(db)
            return db.get("users", [])
    except Exception:
        return []


def create_user(username: str, password: str, display_name: str, email: str = "", role: str = "USER", actor_id: str = "system") -> Dict[str, Any]:
    """Crea un nuevo usuario."""
    with FileLock(str(USERS_LOCK_FILE), timeout=2):
        db = _read_users_db()
        _bootstrap_admin_if_needed(db)
        
        # Verificar que no existe
        if _find_user_by_username(db, username):
            raise ValueError("Usuario ya existe")
        
        salt, pw_hash = _pbkdf2_hash_password(password)
        user_id = secrets.token_urlsafe(12)
        
        user = {
            "id": user_id,
            "username": username.strip(),
            "display_name": display_name.strip(),
            "email": email.strip(),
            "role": _normalize_role(role),
            "role_locked": False,
            "is_active": True,
            "salt": salt,
            "password_hash": pw_hash,
            "password_sig": _sha256_hex(password),
            "dni": "",
            "gender": "",
            "position": "",
            "site_name": "",
            "external_id": None,
            "created_at": _now_ts(),
            "created_by": actor_id
        }
        
        db.setdefault("users", [])
        db["users"].append(user)
        _write_users_db(db)
        
        return user


def update_user_role(user_id: str, new_role: str, actor_id: str) -> Dict[str, Any]:
    """Actualiza el rol de un usuario."""
    with FileLock(str(USERS_LOCK_FILE), timeout=2):
        db = _read_users_db()
        _bootstrap_admin_if_needed(db)
        
        user = _find_user(db, user_id)
        if not user:
            raise ValueError("Usuario no existe")
        
        # Verificar que no se degrade el último admin activo
        if new_role.upper() != "ADMIN" and user.get("role") == "ADMIN":
            # Contar admins activos (excluyendo el que se está modificando)
            other_admins = [u for u in db.get("users", []) 
                          if u.get("id") != user_id 
                          and u.get("is_active", True) 
                          and _is_admin(u)]
            if len(other_admins) == 0:
                raise ValueError("No se puede degradar el último administrador activo")
        
        user["role"] = _normalize_role(new_role)
        # Marcar como bloqueado para que sync no lo sobrescriba
        user["role_locked"] = True
        _write_users_db(db)
        
        return user


def _password_from_dni(dni: str) -> str:
    """Genera contraseña desde DNI."""
    d = str(dni or "").strip()
    return f"{d[::-1]}---{d}" if d else ""


def _sha256_hex(s: str) -> str:
    """Hash SHA256 de un string."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _normalize_employees_payload(payload: Any) -> List[Dict[str, Any]]:
    """Normaliza el payload de empleados desde el backend."""
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
            "role": _normalize_role(it.get("position") or "USER") or "USER",
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
    """Genera hash para detectar cambios en los empleados."""
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


def sync_users_if_stale(force: bool = False) -> Dict[str, Any]:
    """Sincroniza usuarios desde el backend si es necesario."""
    now = _now_ts()

    with FileLock(str(USERS_LOCK_FILE), timeout=2):
        db = _read_users_db()
        _bootstrap_admin_if_needed(db)

        last = float(db.get("employers_last_sync_ts", 0) or 0)
        if (not force) and (now - last) < EMPLOYERS_SYNC_TTL_SECONDS:
            _write_users_db(db)
            return {"status": "fresh", "last_sync_ts": last, "users_count": len(db.get("users") or [])}

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
            _write_users_db(db)
            return {"status": "error", "error": str(e), "last_sync_ts": last}

        employees = _normalize_employees_payload(payload)
        payload_hash = _hash_employees_for_cache(employees)
        old_hash = db.get("employers_last_hash") or ""

        users = db.get("users", [])
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
                    "email": "",
                    "role": emp.get("role") or "USER",
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

                # NO PISAR ROLE si está bloqueado (promovido manualmente)
                incoming_role = _normalize_role(emp.get("role") or existing.get("role") or "USER") or "USER"

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
        sessions = db.get("sessions", {})

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

        db["employers_last_sync_ts"] = now
        db["employers_last_hash"] = payload_hash
        _write_users_db(db)

        return {
            "status": "synced",
            "changed": (payload_hash != old_hash),
            "added": len(added),
            "updated": len(updated),
            "removed": removed_count,
            "password_updates": pw_updated_count,
            "last_sync_ts": now,
            "users_count": len(users),
        }
