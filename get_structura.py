#!/usr/bin/env python3
"""
Exporta un proyecto a 2 JSON en la raíz del proyecto:

1) project_structure.json -> estructura de carpetas/archivos (árbol)
2) project_files.json     -> resumen por archivo (sin volcar contenido completo)
   - Para .py: lista funciones, clases (y sus métodos) y variables (a nivel de módulo).
   - Para otros archivos: solo metadatos (sin contenido).

Ignora carpetas: __pycache__, venv, .venv y TODO lo de git (.git + archivos git).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ✅ Ignorar carpetas y archivos relacionados con Git
DEFAULT_IGNORE_DIRS: Set[str] = {"__pycache__", "venv", ".venv", ".git"}

DEFAULT_IGNORE_FILES: Set[str] = {
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".gitkeep",
    ".git-blame-ignore-revs",
}


# -----------------------------
# Utils lectura / binario
# -----------------------------
def is_binary_file(path: Path, sample_size: int = 4096) -> bool:
    """Heurística simple: si hay byte nulo en el sample, lo tratamos como binario."""
    try:
        with path.open("rb") as f:
            chunk = f.read(sample_size)
        return b"\x00" in chunk
    except Exception:
        # Si no se puede leer, lo tratamos como binario para no romper el proceso.
        return True


def safe_read_text(path: Path, max_bytes: int) -> str:
    """Lee texto (UTF-8) hasta max_bytes, reemplazando caracteres inválidos."""
    with path.open("rb") as f:
        data = f.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _is_ignored_path(path: Path, ignore_dirs: Set[str]) -> bool:
    # Ignora si cualquier parte del path coincide con dir ignorada (incluye .git en cualquier nivel)
    return any(part in ignore_dirs for part in path.parts)


# -----------------------------
# 1) Estructura (árbol)
# -----------------------------
def build_tree(root: Path, ignore_dirs: Set[str], ignore_files: Set[str]) -> Dict[str, Any]:
    """
    Construye un árbol como dict:
    {
      "name": "mi_proyecto",
      "type": "dir",
      "path": ".",
      "children": [...]
    }
    """

    def add_node(parent: Dict[str, Any], rel_parts: List[str], node: Dict[str, Any]) -> None:
        cur = parent
        for part in rel_parts:
            found = None
            for ch in cur.get("children", []):
                if ch["type"] == "dir" and ch["name"] == part:
                    found = ch
                    break
            if found is None:
                found = {
                    "name": part,
                    "type": "dir",
                    "path": str(Path(part)) if cur["path"] == "." else str(Path(cur["path"]) / part),
                    "children": [],
                }
                cur.setdefault("children", []).append(found)
            cur = found
        cur.setdefault("children", []).append(node)

    tree: Dict[str, Any] = {
        "name": root.name,
        "type": "dir",
        "path": ".",
        "children": [],
    }

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # ✅ Filtrar carpetas ignoradas (incluye .git)
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]

        current_dir = Path(dirpath)
        rel_dir = current_dir.relative_to(root)

        for fn in sorted(filenames):
            # ✅ Ignorar archivos de git (y otros que metas en ignore_files)
            if fn in ignore_files:
                continue

            fpath = current_dir / fn
            if _is_ignored_path(fpath, ignore_dirs):
                continue

            rel_file = fpath.relative_to(root)
            node = {
                "name": fn,
                "type": "file",
                "path": rel_file.as_posix(),
                "extension": fpath.suffix.lstrip("."),
            }

            if rel_dir == Path("."):
                tree["children"].append(node)
            else:
                add_node(tree, list(rel_dir.parts), node)

    return tree


# -----------------------------
# 2) Resumen de Python (AST)
# -----------------------------
def _target_names_from_assign(target: ast.AST) -> List[str]:
    """Extrae nombres de variables de targets tipo Name / Tuple / List."""
    out: List[str] = []
    if isinstance(target, ast.Name):
        out.append(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            out.extend(_target_names_from_assign(elt))
    return out


def _short_value_repr(node: Optional[ast.AST], *, max_len: int = 120) -> Optional[str]:
    """Representación corta del valor asignado (si se puede)."""
    if node is None:
        return None
    try:
        # ast.unparse existe en Python 3.9+
        s = ast.unparse(node)  # type: ignore[attr-defined]
    except Exception:
        # fallback simple
        if isinstance(node, ast.Constant):
            s = repr(node.value)
        else:
            return None
    s = s.strip().replace("\n", " ")
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def summarize_python_source(source: str, *, filename: str = "<unknown>") -> Dict[str, Any]:
    """
    Resumen del módulo Python:
    - functions: funciones top-level (incluye async)
    - classes: clases top-level + métodos
    - variables: variables asignadas a nivel de módulo
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return {
            "type": "python",
            "parse_ok": False,
            "error": f"SyntaxError: {e.msg} (line {e.lineno}, col {e.offset})",
            "functions": [],
            "classes": [],
            "variables": [],
        }

    functions: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []
    variables: List[Dict[str, Any]] = []

    for node in tree.body:
        # ---- Funciones top-level
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            kwonly = [a.arg for a in node.args.kwonlyargs]
            has_varargs = node.args.vararg.arg if node.args.vararg else None
            has_kwargs = node.args.kwarg.arg if node.args.kwarg else None

            functions.append(
                {
                    "name": node.name,
                    "lineno": getattr(node, "lineno", None),
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "args": args,
                    "kwonlyargs": kwonly,
                    "vararg": has_varargs,
                    "kwarg": has_kwargs,
                }
            )
            continue

        # ---- Clases top-level
        if isinstance(node, ast.ClassDef):
            methods: List[Dict[str, Any]] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(
                        {
                            "name": item.name,
                            "lineno": getattr(item, "lineno", None),
                            "async": isinstance(item, ast.AsyncFunctionDef),
                        }
                    )

            bases: List[str] = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))  # type: ignore[attr-defined]
                except Exception:
                    if isinstance(b, ast.Name):
                        bases.append(b.id)

            classes.append(
                {
                    "name": node.name,
                    "lineno": getattr(node, "lineno", None),
                    "bases": bases,
                    "methods": methods,
                }
            )
            continue

        # ---- Variables a nivel de módulo
        if isinstance(node, ast.Assign):
            names: List[str] = []
            for t in node.targets:
                names.extend(_target_names_from_assign(t))
            if names:
                variables.append(
                    {
                        "names": sorted(set(names)),
                        "lineno": getattr(node, "lineno", None),
                        "value": _short_value_repr(node.value),
                    }
                )
            continue

        if isinstance(node, ast.AnnAssign):
            # x: int = 1
            names = _target_names_from_assign(node.target)
            if names:
                variables.append(
                    {
                        "names": sorted(set(names)),
                        "lineno": getattr(node, "lineno", None),
                        "value": _short_value_repr(node.value),
                        "annotation": _short_value_repr(node.annotation),
                    }
                )
            continue

        if isinstance(node, ast.AugAssign):
            # x += 1
            names = _target_names_from_assign(node.target)
            if names:
                variables.append(
                    {
                        "names": sorted(set(names)),
                        "lineno": getattr(node, "lineno", None),
                        "op": node.op.__class__.__name__,
                        "value": _short_value_repr(node.value),
                    }
                )
            continue

    return {
        "type": "python",
        "parse_ok": True,
        "functions": functions,
        "classes": classes,
        "variables": variables,
    }


# -----------------------------
# 3) Recolección de archivos (resumen)
# -----------------------------
def collect_files_summary(
    root: Path,
    ignore_dirs: Set[str],
    ignore_files: Set[str],
    max_bytes: int,
) -> List[Dict[str, Any]]:
    """
    Retorna lista de archivos con metadatos y (si aplica) resumen importante.
    """
    out: List[Dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        current_dir = Path(dirpath)

        for fn in sorted(filenames):
            if fn in ignore_files:
                continue

            fpath = current_dir / fn
            if _is_ignored_path(fpath, ignore_dirs):
                continue

            rel_file = fpath.relative_to(root).as_posix()
            ext = fpath.suffix.lstrip(".")

            try:
                size = fpath.stat().st_size
            except Exception:
                size = -1

            binary = is_binary_file(fpath)
            truncated = (size != -1 and size > max_bytes)

            item: Dict[str, Any] = {
                "path": rel_file,
                "name": fn,
                "extension": ext,
                "size_bytes": size,
                "is_binary": binary,
                "truncated": truncated,
            }

            # ✅ Solo “lo importante”: resumen AST para Python, sin contenido completo.
            if (not binary) and fpath.suffix.lower() == ".py":
                try:
                    source = safe_read_text(fpath, max_bytes)
                    item["summary"] = summarize_python_source(source, filename=rel_file)
                except Exception as e:
                    item["summary"] = {
                        "type": "python",
                        "parse_ok": False,
                        "error": f"read/parse error: {e}",
                        "functions": [],
                        "classes": [],
                        "variables": [],
                    }
            else:
                item["summary"] = None

            out.append(item)

    return out


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exporta estructura y resumen importante de un proyecto a JSON.")
    parser.add_argument("project_path", help="Ruta del proyecto (carpeta raíz a escanear).")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=1_000_000,
        help="Máximo de bytes a leer por archivo (default: 1,000,000). Si el archivo es más grande, se trunca.",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Nombre de carpeta adicional a ignorar (puedes repetir).",
    )
    parser.add_argument(
        "--ignore-file",
        action="append",
        default=[],
        help="Nombre de archivo adicional a ignorar (puedes repetir).",
    )
    args = parser.parse_args()

    root = Path(args.project_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"ERROR: La ruta no existe o no es carpeta: {root}")

    ignore_dirs = set(DEFAULT_IGNORE_DIRS) | set(args.ignore)
    ignore_files = set(DEFAULT_IGNORE_FILES) | set(args.ignore_file)

    structure = build_tree(root, ignore_dirs, ignore_files)
    files_summary = collect_files_summary(root, ignore_dirs, ignore_files, args.max_bytes)

    structure_path = root / "project_structure.json"
    files_path = root / "project_files.json"

    write_json(structure_path, structure)
    write_json(files_path, files_summary)

    print(f"OK -> {structure_path}")
    print(f"OK -> {files_path}")
    print(f"Ignoradas dirs: {sorted(ignore_dirs)}")
    print(f"Ignorados files: {sorted(ignore_files)}")
    print(f"Archivos exportados: {len(files_summary)} (max_bytes={args.max_bytes})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
