#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Reflection Engine v1.0 – исправленная версия
Периодический анализ графа памяти: поиск противоречий, автоматическая верификация,
корректировка confidence фактов.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Set
from collections import defaultdict
from contextlib import contextmanager

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx,
    normalize_path, _ensure_allowed
)
from mcp_memory_graph import _graph_db, GRAPH_DB_PATH, SimpleExtractor

# ========== Конфигурация ==========
REFLECTION_INTERVAL_MIN = int(os.environ.get("MCP_REFLECTION_INTERVAL_MIN", "30"))
REFLECTION_AUTO_RUN = os.environ.get("MCP_REFLECTION_AUTO_RUN", "true").lower() == "true"
VERIFICATION_SOURCES_THRESHOLD = int(os.environ.get("MCP_VERIFICATION_SOURCES", "3"))
MAX_ENTRIES_PER_REFLECTION = int(os.environ.get("MCP_REFLECTION_MAX_ENTRIES", "500"))

# ========== Таблица рефлексий ==========
def _init_reflection_table():
    with _graph_db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                reflection_id TEXT PRIMARY KEY,
                source_entry_id TEXT,
                target_entry_id TEXT,
                reflection_type TEXT,
                description TEXT,
                confidence_delta REAL,
                created_at REAL,
                resolved INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reflection_source ON reflections(source_entry_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reflection_target ON reflections(target_entry_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reflection_created ON reflections(created_at)")
        conn.commit()

_init_reflection_table()

def _add_reflection(source_entry_id: str, target_entry_id: str,
                    reflection_type: str, description: str,
                    confidence_delta: float = 0.0):
    import hashlib
    ref_id = hashlib.md5(f"{source_entry_id}_{target_entry_id}_{time.time()}".encode()).hexdigest()[:16]
    now = time.time()
    with _graph_db._get_conn() as conn:
        conn.execute("""
            INSERT INTO reflections
            (reflection_id, source_entry_id, target_entry_id, reflection_type,
             description, confidence_delta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ref_id, source_entry_id, target_entry_id, reflection_type,
              description, confidence_delta, now))
        conn.commit()

def _update_entry_confidence(entry_id: str, new_confidence: float,
                             verification_status: str = None):
    try:
        conn = conversation_memory._get_conn()
        if verification_status:
            conn.execute(
                "UPDATE entries SET confidence = ?, verification_status = ? WHERE id = ?",
                (new_confidence, verification_status, entry_id)
            )
        else:
            conn.execute(
                "UPDATE entries SET confidence = ? WHERE id = ?",
                (new_confidence, entry_id)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        _log(f"[Reflection] Failed to update entry {entry_id}: {e}")

def _find_version_conflicts(limit: int = 100) -> List[Dict]:
    conflicts = []
    with _graph_db._get_conn() as conn:
        entities = conn.execute(
            "SELECT entity_id, name, type FROM entities WHERE type IN ('file', 'model') ORDER BY name"
        ).fetchall()

    groups = defaultdict(list)
    for e in entities:
        name = e[1]
        import re
        base = re.sub(r'[\s_\-]*v?\d+(?:\.\d+)*', '', name, flags=re.IGNORECASE).strip()
        groups[base].append(e)

    for base, group in groups.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: x[1])
        oldest_name = group_sorted[0][1]
        newest_name = group_sorted[-1][1]
        with _graph_db._get_conn() as conn:
            for ent in group_sorted:
                beliefs = conn.execute(
                    "SELECT belief_id, statement, confidence, source_entry_id as entry_id FROM beliefs WHERE statement LIKE ?",
                    (f'%{ent[1]}%',)
                ).fetchall()
                if len(beliefs) >= 2:
                    conflicts.append({
                        "type": "version_conflict",
                        "entity": ent[1],
                        "base_name": base,
                        "old_version": oldest_name,
                        "new_version": newest_name,
                        "beliefs": [dict(b) for b in beliefs],
                        "description": f"Версионный конфликт для {base}: {oldest_name} vs {newest_name}"
                    })
    return conflicts

def _find_relation_conflicts(limit: int = 100) -> List[Dict]:
    conflicts = []
    with _graph_db._get_conn() as conn:
        rows = conn.execute("""
            SELECT source_id, relation_type, target_id, confidence, source_entry_id
            FROM relations
            ORDER BY source_id, relation_type
        """).fetchall()

    by_source = defaultdict(list)
    for r in rows:
        by_source[r[0]].append(r)

    for source_id, rels in by_source.items():
        by_type = defaultdict(set)
        for r in rels:
            by_type[r[1]].add((r[2], r[3], r[4]))
        for rel_type, targets in by_type.items():
            if len(targets) > 1:
                conflicts.append({
                    "type": "relation_conflict",
                    "source_id": source_id,
                    "relation_type": rel_type,
                    "targets": list(targets),
                    "description": f"Сущность {source_id} имеет несколько {rel_type}: {targets}"
                })
    return conflicts

def _find_contradictory_beliefs(limit: int = 100) -> List[Dict]:
    antonym_pairs = [
        ("true", "false"), ("yes", "no"), ("correct", "incorrect"),
        ("exists", "not exists"), ("available", "unavailable"),
        ("success", "failure"), ("ok", "error")
    ]
    conflicts = []
    with _graph_db._get_conn() as conn:
        beliefs = conn.execute(
            "SELECT belief_id, statement, confidence, source_entry_id as entry_id, verification_status FROM beliefs"
        ).fetchall()

    for i in range(len(beliefs)):
        for j in range(i+1, len(beliefs)):
            stmt1 = beliefs[i][1].lower()
            stmt2 = beliefs[j][1].lower()
            for a, b in antonym_pairs:
                if (a in stmt1 and b in stmt2) or (b in stmt1 and a in stmt2):
                    conflicts.append({
                        "type": "antonym_conflict",
                        "belief1": dict(beliefs[i]),
                        "belief2": dict(beliefs[j]),
                        "description": f"Противоречие: '{beliefs[i][1]}' vs '{beliefs[j][1]}'"
                    })
                    break
    return conflicts

def auto_verify_beliefs():
    with _graph_db._get_conn() as conn:
        rows = conn.execute(
            "SELECT belief_id, statement, confidence, source_entry_id as entry_id, verification_status FROM beliefs"
        ).fetchall()

    stmt_to_beliefs = defaultdict(list)
    for b in rows:
        stmt_to_beliefs[b[1]].append(b)

    for stmt, beliefs in stmt_to_beliefs.items():
        if len(beliefs) >= VERIFICATION_SOURCES_THRESHOLD:
            for b in beliefs:
                if b[4] != "verified":
                    new_conf = min(1.0, b[2] + 0.1 * len(beliefs))
                    _update_entry_confidence(b[3], new_conf, "verified")
                    with _graph_db._get_conn() as conn:
                        conn.execute(
                            "UPDATE beliefs SET confidence = ?, verification_status = ? WHERE belief_id = ?",
                            (new_conf, "verified", b[0])
                        )
                        conn.commit()
                    _log(f"[Reflection] Auto-verified belief '{stmt[:50]}...' with {len(beliefs)} sources")

def run_reflection(limit: int = MAX_ENTRIES_PER_REFLECTION):
    _log("[Reflection] Starting reflection cycle...")
    start_time = time.time()

    version_conflicts = _find_version_conflicts(limit)
    relation_conflicts = _find_relation_conflicts(limit)
    contradictory_beliefs = _find_contradictory_beliefs(limit)

    total_conflicts = len(version_conflicts) + len(relation_conflicts) + len(contradictory_beliefs)
    _log(f"[Reflection] Found {total_conflicts} conflicts: "
         f"{len(version_conflicts)} version, {len(relation_conflicts)} relation, "
         f"{len(contradictory_beliefs)} antonym")

    for conf in version_conflicts:
        for belief in conf["beliefs"]:
            entry_id = belief["entry_id"]
            if belief["statement"] == conf["old_version"]:
                old_conf = belief["confidence"]
                new_conf = max(0.1, old_conf - 0.3)
                _update_entry_confidence(entry_id, new_conf)
                _add_reflection(
                    source_entry_id=entry_id,
                    target_entry_id=None,
                    reflection_type="version_conflict",
                    description=f"Версия {conf['old_version']} устарела, заменена на {conf['new_version']}",
                    confidence_delta=-0.3
                )
            elif belief["statement"] == conf["new_version"]:
                old_conf = belief["confidence"]
                new_conf = min(1.0, old_conf + 0.2)
                _update_entry_confidence(entry_id, new_conf)
                _add_reflection(
                    source_entry_id=entry_id,
                    target_entry_id=None,
                    reflection_type="version_conflict",
                    description=f"Версия {conf['new_version']} актуальна",
                    confidence_delta=+0.2
                )

    for conf in relation_conflicts:
        for target, conf_val, entry_id in conf["targets"]:
            if conf_val < 0.7:
                _update_entry_confidence(entry_id, max(0.1, conf_val - 0.2))
                _add_reflection(
                    source_entry_id=entry_id,
                    target_entry_id=None,
                    reflection_type="relation_conflict",
                    description=f"Конфликт отношения {conf['relation_type']}: {target}",
                    confidence_delta=-0.2
                )

    for conf in contradictory_beliefs:
        b1 = conf["belief1"]
        b2 = conf["belief2"]
        _update_entry_confidence(b1["entry_id"], max(0.1, b1["confidence"] - 0.3))
        _update_entry_confidence(b2["entry_id"], max(0.1, b2["confidence"] - 0.3))
        _add_reflection(
            source_entry_id=b1["entry_id"],
            target_entry_id=b2["entry_id"],
            reflection_type="antonym_conflict",
            description=conf["description"],
            confidence_delta=-0.3
        )

    auto_verify_beliefs()

    elapsed = time.time() - start_time
    _log(f"[Reflection] Reflection cycle completed in {elapsed:.2f}s")

_scheduler_job_id = None

def start_reflection_scheduler():
    if not REFLECTION_AUTO_RUN:
        _log("[Reflection] Auto-run disabled, scheduler not started.")
        return
    try:
        from mcp_scheduler import scheduler_add_interval
        global _scheduler_job_id
        if _scheduler_job_id:
            pass
        result = scheduler_add_interval(
            name="reflection_engine",
            tool_name="run_reflection",
            interval_seconds=REFLECTION_INTERVAL_MIN * 60,
            args={}
        )
        _scheduler_job_id = result.get("job_id")
        _log(f"[Reflection] Scheduler started, interval={REFLECTION_INTERVAL_MIN} min")
    except ImportError:
        _log("[Reflection] mcp_scheduler not available, will not run periodically.")
    except Exception as e:
        _log(f"[Reflection] Failed to start scheduler: {e}")

def tool_run_reflection(limit: int = MAX_ENTRIES_PER_REFLECTION) -> Dict:
    run_reflection(limit)
    return {"status": "reflection_completed", "limit": limit}

def tool_get_reflections(limit: int = 50) -> Dict:
    with _graph_db._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return {
            "status": "success",
            "reflections": [dict(r) for r in rows],
            "count": len(rows)
        }

def tool_reflection_stats() -> Dict:
    with _graph_db._get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        unresolved = conn.execute("SELECT COUNT(*) FROM reflections WHERE resolved = 0").fetchone()[0]
        return {
            "total_reflections": total,
            "unresolved": unresolved,
            "auto_verify_threshold": VERIFICATION_SOURCES_THRESHOLD,
            "interval_min": REFLECTION_INTERVAL_MIN,
            "auto_run": REFLECTION_AUTO_RUN
        }

server = BaseMCPServer("reflection-engine", "1.0")

server.register_tool("run_reflection", {
    "description": "Запустить анализ противоречий в графе памяти и автоматическую верификацию фактов",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": MAX_ENTRIES_PER_REFLECTION}
        }
    }
}, lambda **kw: tool_run_reflection(kw.get("limit", MAX_ENTRIES_PER_REFLECTION)))

server.register_tool("get_reflections", {
    "description": "Получить список записей рефлексии (противоречий, корректировок)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 50}
        }
    }
}, lambda **kw: tool_get_reflections(kw.get("limit", 50)))

server.register_tool("reflection_stats", {
    "description": "Статистика работы рефлексии",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_reflection_stats())

if REFLECTION_AUTO_RUN:
    threading.Thread(target=run_reflection, daemon=True).start()
    start_reflection_scheduler()

__mcp_plugin__ = {
    "name": "reflection-engine",
    "version": "1.0",
    "description": "Периодический анализ графа памяти, поиск противоречий, авто-верификация",
    "dependencies": [],
    "on_load": lambda: _log("[Reflection] v1.0 loaded. Auto-run enabled."),
    "on_unload": lambda: _log("[Reflection] Unloaded.")
}

if __name__ == "__main__":
    server.run()