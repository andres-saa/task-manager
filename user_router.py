"""
Router para gesti?n de usuarios y autenticaci?n.
"""
import secrets
from typing import Optional
from fastapi import APIRouter, Request, Form, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import (
    get_current_user, require_user, require_admin,
    get_all_users, create_user, update_user_role,
    _find_user_by_username, _read_users_db, _write_users_db,
    _verify_password, _login_user, _logout_sid, _is_admin,
    _bootstrap_admin_if_needed, sync_users_if_stale,
    get_user_avatar_url,
    SESSION_COOKIE, USERS_LOCK_FILE, BOOTSTRAP_ADMIN_USER, BOOTSTRAP_ADMIN_PASS
)
from filelock import FileLock
from pathlib import Path

# Asegurar que el directorio existe
Path(USERS_LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="", tags=["Users"])
templates = Jinja2Templates(directory="templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, username: Optional[str] = None, password: Optional[str] = None):
    """P?gina de login. Soporta login autom?tico con par?metros URL desde otras apps."""
    # Sincronizar usuarios si es necesario
    sync_users_if_stale(force=False)
    
    # Si hay credenciales en la URL, primero desautenticar al usuario anterior (si existe)
    if username and password:
        # Cerrar sesi?n anterior si existe
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            try:
                with FileLock(str(USERS_LOCK_FILE), timeout=2):
                    db = _read_users_db()
                    _logout_sid(db, sid)
                    _write_users_db(db)
            except Exception:
                pass
        
        # Intentar login autom?tico (sin mostrar interfaz)
        try:
            with FileLock(str(USERS_LOCK_FILE), timeout=2):
                db = _read_users_db()
                _bootstrap_admin_if_needed(db)
                
                user = _find_user_by_username(db, username)
                if user and user.get("is_active", True):
                    if _verify_password(password, user.get("salt", ""), user.get("password_hash", "")):
                        sid = _login_user(db, user["id"])
                        _write_users_db(db)
                        
                        resp = RedirectResponse(url="/", status_code=302)
                        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
                        return resp
        except Exception:
            pass
        
        # Si falla el login con par?metros, redirigir al master
        return RedirectResponse(url="https://gestion.salchimonster.com/tasks/", status_code=302)
    
    # Si no hay par?metros, verificar si ya est? logueado
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    
    # Si no hay par?metros y no est? logueado, mostrar interfaz de login normal
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    """Procesa el login."""
    # Sincronizar usuarios si es necesario
    sync_users_if_stale(force=False)
    
    with FileLock(str(USERS_LOCK_FILE), timeout=2):
        db = _read_users_db()
        
        user = _find_user_by_username(db, username)
        if not user or not user.get("is_active", True):
            # Si falla, redirigir al master
            return RedirectResponse(url="https://gestion.salchimonster.com/tasks/", status_code=302)
        
        if not _verify_password(password, user.get("salt", ""), user.get("password_hash", "")):
            # Si falla, redirigir al master
            return RedirectResponse(url="https://gestion.salchimonster.com/tasks/", status_code=302)
        
        sid = _login_user(db, user["id"])
        _write_users_db(db)
    
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@router.get("/logout")
async def logout_action(request: Request):
    """Cierra sesi?n y redirige al master."""
    sid = request.cookies.get(SESSION_COOKIE)
    
    if sid:
        with FileLock(str(USERS_LOCK_FILE), timeout=2):
            db = _read_users_db()
            _logout_sid(db, sid)
            _write_users_db(db)
    
    # Redirigir al master despu?s del logout
    resp = RedirectResponse(url="https://gestion.salchimonster.com/tasks/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/admin/login")
async def admin_login_direct(request: Request):
    """Login directo como admin usando credenciales por defecto.
    
    Link directo: http://localhost:8000/admin/login
    """
    # Cerrar sesi?n anterior si existe
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        try:
            with FileLock(str(USERS_LOCK_FILE), timeout=2):
                db = _read_users_db()
                _logout_sid(db, sid)
                _write_users_db(db)
        except Exception:
            pass
    
    # Login autom?tico como admin usando credenciales por defecto
    try:
        with FileLock(str(USERS_LOCK_FILE), timeout=2):
            db = _read_users_db()
            _bootstrap_admin_if_needed(db)
            
            # Buscar usuario admin
            admin_user = _find_user_by_username(db, BOOTSTRAP_ADMIN_USER)
            
            if admin_user and admin_user.get("is_active", True):
                # Verificar contrase?a por defecto
                if _verify_password(BOOTSTRAP_ADMIN_PASS, admin_user.get("salt", ""), admin_user.get("password_hash", "")):
                    sid = _login_user(db, admin_user["id"])
                    _write_users_db(db)
                    
                    resp = RedirectResponse(url="/", status_code=302)
                    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
                    return resp
    except Exception:
        pass
    
    # Si falla, redirigir a login normal
    return RedirectResponse(url="/login", status_code=302)


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    """P?gina de gesti?n de usuarios (solo admin)."""
    user = require_admin(request)
    users = get_all_users()
    
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "user": user,
        "users": users
    })


@router.get("/admin/teams", response_class=HTMLResponse)
async def admin_teams_page(request: Request):
    """P?gina de gesti?n de equipos (solo admin)."""
    user = require_admin(request)
    from storage import load_teams
    teams_models = load_teams()
    teams = [t.model_dump() for t in teams_models]
    users = get_all_users()
    
    # Incluir dni, site_name y avatar_url en usuarios
    users_with_extra = []
    for u in users:
        users_with_extra.append({
            **u,
            "dni": u.get("dni", ""),
            "site_name": u.get("site_name", ""),
            "avatar_url": get_user_avatar_url(u)
        })
    
    return templates.TemplateResponse("admin_teams.html", {
        "request": request,
        "user": user,
        "teams": teams,
        "users": users_with_extra
    })


@router.post("/api/users/create")
async def api_create_user(request: Request, data: dict = Body(...)):
    """Crea un nuevo usuario (solo admin). DESHABILITADO - Solo sincronizaci?n desde backend."""
    raise HTTPException(status_code=403, detail="La creaci?n manual de usuarios est? deshabilitada. Use la sincronizaci?n desde el backend.")


@router.post("/api/users/{user_id}/promote")
async def api_promote_user(user_id: str, request: Request, data: dict = Body(...)):
    """Promueve un usuario a admin (solo admin)."""
    actor = require_admin(request)
    
    new_role = (data.get("role") or "ADMIN").strip().upper()
    if new_role not in ("ADMIN", "MANAGER", "USER"):
        raise HTTPException(status_code=400, detail="Rol inv?lido")
    
    try:
        user = update_user_role(user_id, new_role, actor["id"])
        return JSONResponse({"status": "ok", "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"]
        }})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/users")
async def api_list_users(request: Request):
    """Lista todos los usuarios."""
    _ = require_user(request)
    # Sincronizar usuarios si es necesario
    sync_users_if_stale(force=False)
    users = get_all_users()
    
    # Ocultar informaci?n sensible pero incluir datos necesarios para filtros y fotos
    safe_users = []
    for u in users:
        safe_users.append({
            "id": u.get("id"),
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "email": u.get("email"),
            "role": u.get("role"),
            "site_name": u.get("site_name", ""),
            "dni": u.get("dni", ""),
            "gender": u.get("gender", ""),
            "avatar_url": get_user_avatar_url(u),
            "is_active": u.get("is_active", True)
        })
    
    return JSONResponse({"users": safe_users})


@router.post("/api/users/sync")
async def api_sync_users(request: Request):
    """Sincroniza usuarios desde el backend (solo admin)."""
    _ = require_admin(request)
    sync_result = sync_users_if_stale(force=True)
    return JSONResponse({"sync": sync_result})


@router.get("/documentation", response_class=HTMLResponse)
async def documentation_page(request: Request):
    """P?gina de documentaci?n del sistema."""
    # No requiere autenticaci?n, es documentaci?n p?blica
    return templates.TemplateResponse("documentation.html", {"request": request})


@router.get("/documentation/user", response_class=HTMLResponse)
async def documentation_user_page(request: Request):
    """P?gina de documentaci?n simplificada para usuarios no t?cnicos."""
    # No requiere autenticaci?n, es documentaci?n p?blica
    return templates.TemplateResponse("documentation_user.html", {"request": request})
