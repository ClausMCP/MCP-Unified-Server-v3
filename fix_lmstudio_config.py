#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генерирует конфигурацию для LM Studio.
Поддерживает полностью портативный режим (tools/python).
ВСЕГДА использует mcp_fs_server.py (unified), чтобы видеть все инструменты.
"""

import json
import os
import shutil
from pathlib import Path
from datetime import datetime

def generate_correct_config():
    script_dir = Path(__file__).parent
    
    # 1. Find Python executable
    venv_python = script_dir / ".venv" / "Scripts" / "python.exe"
    tools_python = script_dir / "tools" / "python" / "python.exe"
    
    if venv_python.exists():
        python_path = str(venv_python)
    elif tools_python.exists():
        python_path = str(tools_python)
    else:
        raise FileNotFoundError("Python not found in .venv or tools/python")
        
    # 2. ВСЕГДА используем unified сервер (mcp_fs_server.py)
    server_script = script_dir / "mcp_fs_server.py"
    if not server_script.exists():
        # fallback на оркестратор, если unified вдруг отсутствует (не должно)
        server_script = script_dir / "mcp_orchestrator.py"
        if not server_script.exists():
            raise FileNotFoundError("Neither mcp_fs_server.py nor mcp_orchestrator.py found")
            
    # 3. Build portable PATH
    tools_dir = script_dir / "tools"
    path_parts = [
        str(tools_dir / "python"),
        str(tools_dir / "tesseract"),
        str(tools_dir / "ffmpeg"),
        str(tools_dir / "pandoc"),
        str(tools_dir / "wkhtmltopdf"),
    ]
    portable_path = os.pathsep.join([p for p in path_parts if Path(p).exists()])
    if portable_path:
        portable_path += os.pathsep + os.environ.get("PATH", "")
        
    config = {
        "mcpServers": {
            "mcp_unified": {
                "command": str(python_path).replace("\\", "\\\\"),
                "args": [str(server_script).replace("\\", "\\\\")],
                "env": {
                    "PYTHONIOENCODING": "utf-8",
                    "PATH": portable_path.replace("\\", "\\\\"),
                    "MCP_MEMORY_PATH": str(script_dir / "mcp_memory.db").replace("\\", "\\\\"),
                    "MCP_OFFLINE_MODE": "auto",
                    "MCP_AUTO_INDEX_FOLDERS": "",
                    "MCP_WEB_CACHE_TTL": "168",
                    "MCP_INDEX_INTERVAL_HOURS": "6",
                    "MCP_AUTO_INDEX_SEARCH": "true"
                }
            }
        }
    }
    return config

def write_lmstudio_config(config):
    lm_studio_dir = Path.home() / ".lmstudio"
    lm_studio_dir.mkdir(parents=True, exist_ok=True)
    config_path = lm_studio_dir / "mcp.json"
    
    if config_path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = config_path.with_suffix(config_path.suffix + f".backup_{ts}")
        shutil.copy2(config_path, backup_path)
        print(f"💾 Backup created: {backup_path}")
        
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        
    print(f"✅ LM Studio config successfully written to: {config_path}")
    print("   Now restart LM Studio and select profile 'mcp_unified' to see all 247 tools.")

if __name__ == "__main__":
    try:
        cfg = generate_correct_config()
        print("Config generated (using mcp_fs_server.py):")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        write_lmstudio_config(cfg)
    except Exception as e:
        print(f"❌ Error: {e}")
        input("\nPress Enter to exit...")