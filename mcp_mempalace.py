#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP memPalace Integration v1.1
Обеспечивает доступ к внешнему пакету mempalace через CLI.
Поддерживает инициализацию, индексацию и поиск.
"""
import subprocess
import json
import os
import hashlib
from pathlib import Path
from typing import Dict, Optional, List
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, normalize_path, _ensure_allowed
)

__mcp_plugin__ = {
    "name": "mempalace",
    "version": "1.1.0",
    "description": "Интеграция с memPalace – семантическая память для кода и чатов",
    "dependencies": ["mempalace"],
    "on_load": lambda: _log("[mempalace] Loaded. Use mempalace_init/mine/search."),
    "on_unload": lambda: _log("[mempalace] Unloaded.")
}

def _run_mempalace(args: List[str], timeout: int = 300) -> Dict:
    """Выполняет команду mempalace и возвращает результат."""
    try:
        proc = subprocess.run(
            ["mempalace"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='replace'
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "success": proc.returncode == 0
        }
    except FileNotFoundError:
        return {"success": False, "error": "mempalace not installed. Run: pip install mempalace"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _get_folder_hash(folder_path: str) -> str:
    """Вычисляет хеш на основе имён, mtime и размеров всех файлов в папке."""
    hasher = hashlib.md5()
    try:
        for root, dirs, files in os.walk(folder_path):
            # Сортируем для детерминизма
            for f in sorted(files):
                file_path = os.path.join(root, f)
                try:
                    stat = os.stat(file_path)
                    # Учитываем имя файла, mtime и размер
                    hasher.update(f.encode())
                    hasher.update(str(stat.st_mtime).encode())
                    hasher.update(str(stat.st_size).encode())
                except Exception:
                    pass
    except Exception:
        pass
    return hasher.hexdigest()

def mempalace_init(project_path: str) -> Dict:
    """Инициализирует дворец памяти в указанной директории."""
    d_id = dialog_ctx.get()
    path = Path(normalize_path(project_path))
    try:
        _ensure_allowed(path, "mempalace_init")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}
    
    result = _run_mempalace(["init", str(path)])
    if result["success"]:
        conversation_memory.add(
            op="mempalace_init",
            paths={"project": str(path)},
            status="success",
            dialog=d_id,
            context=f"Initialized memPalace in {path}"
        )
        return {"status": "success", "project": str(path), "output": result["stdout"]}
    else:
        return {"status": "error", "error": result.get("error") or result["stderr"]}

def mempalace_mine(path: Optional[str] = None, mode: str = "files", force: bool = False) -> Dict:
    """
    Индексирует файлы или чаты.
    Если path не указан, используется MCP_MEMPALACE_PROJECT или текущая директория.
    force=True – игнорировать хеш и принудительно индексировать.
    """
    if path is None:
        path = os.environ.get("MCP_MEMPALACE_PROJECT")
        if not path:
            path = os.getcwd()
    
    d_id = dialog_ctx.get()
    path_obj = Path(normalize_path(path))
    try:
        _ensure_allowed(path_obj, "mempalace_mine")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}

    # --- Проверка хеша (если не force) ---
    hash_file = path_obj / ".mempalace_hash"
    current_hash = _get_folder_hash(str(path_obj))
    
    if not force and hash_file.exists():
        try:
            with open(hash_file, 'r') as f:
                last_hash = f.read().strip()
            if last_hash == current_hash:
                return {
                    "status": "skipped",
                    "message": "No changes detected, indexing skipped",
                    "path": str(path_obj),
                    "mode": mode
                }
        except Exception:
            pass

    result = _run_mempalace(["mine", str(path_obj), "--mode", mode], timeout=600)
    if result["success"]:
        # Сохраняем новый хеш
        try:
            with open(hash_file, 'w') as f:
                f.write(current_hash)
        except Exception:
            pass
            
        conversation_memory.add(
            op="mempalace_mine",
            paths={"path": str(path_obj), "mode": mode},
            status="success",
            dialog=d_id,
            context=f"Indexed {mode} in {path_obj}"
        )
        return {"status": "success", "path": str(path_obj), "mode": mode, "output": result["stdout"]}
    else:
        return {"status": "error", "error": result.get("error") or result["stderr"]}

def mempalace_search(query: str, project_path: Optional[str] = None,
                     limit: int = 10, mode: str = "all") -> Dict:
    """
    Поиск по памяти memPalace.
    mode: "all", "code", "convos", "docs"
    """
    d_id = dialog_ctx.get()
    args = ["search", query, "--limit", str(limit), "--mode", mode]
    if project_path:
        p = Path(normalize_path(project_path))
        try:
            _ensure_allowed(p, "mempalace_search")
        except PermissionError as e:
            return {"status": "error", "message": str(e)}
        args.extend(["--project", str(p)])

    result = _run_mempalace(args, timeout=60)
    if not result["success"]:
        return {"status": "error", "error": result.get("error") or result["stderr"]}
    
    # Пытаемся распарсить вывод как JSON (если memPalace поддерживает)
    try:
        data = json.loads(result["stdout"])
        results = data.get("results", [])
    except json.JSONDecodeError:
        # Если не JSON, возвращаем сырой текст
        results = [{"text": result["stdout"]}]

    conversation_memory.add(
        op="mempalace_search",
        paths={"query": query, "project": project_path},
        status="success",
        dialog=d_id,
        context=f"Found {len(results)} results in memPalace"
    )
    
    return {
        "status": "success",
        "query": query,
        "count": len(results),
        "results": results[:limit],
        "raw_output": result["stdout"]
    }

def mempalace_status(project_path: Optional[str] = None) -> Dict:
    """Показывает статус дворца памяти."""
    args = ["status"]
    if project_path:
        p = Path(normalize_path(project_path))
        try:
            _ensure_allowed(p, "mempalace_status")
        except PermissionError as e:
            return {"status": "error", "message": str(e)}
        args.extend(["--project", str(p)])

    result = _run_mempalace(args)
    if result["success"]:
        return {"status": "success", "info": result["stdout"]}
    else:
        return {"status": "error", "error": result.get("error") or result["stderr"]}

def register_tools(server: BaseMCPServer):
    server.register_tool("mempalace_init", {
        "description": "Инициализировать хранилище memPalace в проекте",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string"}},
            "required": ["project_path"]
        }
    }, lambda **kw: mempalace_init(kw["project_path"]))

    server.register_tool("mempalace_mine", {
        "description": "Индексировать файлы или чаты с помощью memPalace. Если path не указан, используется MCP_MEMPALACE_PROJECT. force=True для принудительной переиндексации.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к проекту (опционально)"},
                "mode": {"type": "string", "enum": ["files", "convos"], "default": "files"},
                "force": {"type": "boolean", "default": False, "description": "Принудительная индексация без проверки хеша"}
            },
            "required": []
        }
    }, lambda **kw: mempalace_mine(kw.get("path"), kw.get("mode", "files"), kw.get("force", False)))

    server.register_tool("mempalace_search", {
        "description": "Поиск по памяти memPalace (код, чаты, документы)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project_path": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "mode": {"type": "string", "enum": ["all", "code", "convos", "docs"], "default": "all"}
            },
            "required": ["query"]
        }
    }, lambda **kw: mempalace_search(
        kw["query"], kw.get("project_path"), kw.get("limit", 10), kw.get("mode", "all")
    ))

    server.register_tool("mempalace_status", {
        "description": "Показать статус дворца памяти",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string"}}
        }
    }, lambda **kw: mempalace_status(kw.get("project_path")))