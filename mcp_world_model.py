#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP World Model v1.0
Система правил и предсказаний. Хранит правила вида "ЕСЛИ условие ТО вывод".
Использует факты из графа для вывода новых утверждений и предсказаний.
"""

import os
import json
import sqlite3
import hashlib
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from contextlib import contextmanager

from mcp_shared import _log, BaseMCPServer, dialog_ctx

WORLD_MODEL_DB = os.environ.get("MCP_WORLD_MODEL_DB", os.path.join(os.path.dirname(__file__), "world_model.db"))

class WorldModelDB:
    def __init__(self, db_path: str = WORLD_MODEL_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    rule_id TEXT PRIMARY KEY,
                    condition TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    confidence REAL DEFAULT 0.7,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    source_rule_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status)")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def add_rule(self, rule_id: str, condition: Dict, conclusion: str, confidence: float = 0.7) -> bool:
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO rules (rule_id, condition, conclusion, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (rule_id, json.dumps(condition), conclusion, confidence, now))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_rules(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY confidence DESC").fetchall()
            return [dict(r) for r in rows]

    def delete_rule(self, rule_id: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM rules WHERE rule_id = ?", (rule_id,))
            conn.commit()

    def add_prediction(self, statement: str, confidence: float, source_rule_id: str = None, ttl_seconds: int = 3600) -> str:
        pred_id = hashlib.md5(f"{statement}_{datetime.now().timestamp()}".encode()).hexdigest()[:12]
        now = datetime.now().timestamp()
        expires = now + ttl_seconds
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO predictions (prediction_id, statement, confidence, source_rule_id, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (pred_id, statement, confidence, source_rule_id, now, expires, 'active'))
            conn.commit()
        return pred_id

    def get_active_predictions(self, limit: int = 50) -> List[Dict]:
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM predictions
                WHERE status = 'active' AND expires_at > ?
                ORDER BY confidence DESC, created_at DESC LIMIT ?
            """, (now, limit)).fetchall()
            return [dict(r) for r in rows]

    def mark_prediction_outdated(self, prediction_id: str):
        with self._get_conn() as conn:
            conn.execute("UPDATE predictions SET status = 'outdated' WHERE prediction_id = ?", (prediction_id,))
            conn.commit()

class WorldModel:
    def __init__(self):
        self.db = WorldModelDB()
        self._graph_available = False
        try:
            from mcp_memory_graph import _graph_db
            self.graph_db = _graph_db
            self._graph_available = True
        except ImportError:
            _log("[WorldModel] Memory graph not available, will work without fact checks")

    def evaluate_condition(self, condition: Dict) -> bool:
        if not self._graph_available:
            return False
        cond_type = condition.get("type")
        if cond_type == "fact":
            statement = condition.get("statement", "")
            with self.graph_db._get_conn() as conn:
                row = conn.execute(
                    "SELECT confidence FROM facts WHERE statement LIKE ? AND confidence > 0.8 LIMIT 1",
                    (f"%{statement}%",)
                ).fetchone()
                return row is not None
        elif cond_type == "entity_exists":
            entity_name = condition.get("name", "")
            with self.graph_db._get_conn() as conn:
                row = conn.execute("SELECT 1 FROM entities WHERE name = ?", (entity_name,)).fetchone()
                return row is not None
        elif cond_type == "relation_exists":
            source = condition.get("source")
            rel = condition.get("relation")
            target = condition.get("target")
            if not all([source, rel, target]):
                return False
            with self.graph_db._get_conn() as conn:
                row = conn.execute("""
                    SELECT 1 FROM relations r
                    JOIN entities e1 ON r.source_id = e1.entity_id
                    JOIN entities e2 ON r.target_id = e2.entity_id
                    WHERE e1.name = ? AND r.relation_type = ? AND e2.name = ?
                """, (source, rel, target)).fetchone()
                return row is not None
        else:
            return False

    def run_forward_chaining(self) -> List[Dict]:
        rules = self.db.get_rules()
        new_predictions = []
        for rule in rules:
            condition = json.loads(rule["condition"])
            if self.evaluate_condition(condition):
                conclusion = rule["conclusion"]
                existing = [p for p in self.db.get_active_predictions(limit=100) if p["statement"] == conclusion]
                if not existing:
                    pred_id = self.db.add_prediction(
                        statement=conclusion,
                        confidence=rule["confidence"],
                        source_rule_id=rule["rule_id"],
                        ttl_seconds=3600
                    )
                    new_predictions.append({
                        "prediction_id": pred_id,
                        "statement": conclusion,
                        "confidence": rule["confidence"],
                        "source_rule_id": rule["rule_id"]
                    })
        return new_predictions

    def get_predictions(self, limit: int = 50) -> List[Dict]:
        return self.db.get_active_predictions(limit)

_world_model = WorldModel()

def world_add_rule(condition: Dict, conclusion: str, confidence: float = 0.7, rule_id: str = None) -> Dict:
    if not rule_id:
        rule_id = hashlib.md5(f"{json.dumps(condition)}_{conclusion}".encode()).hexdigest()[:12]
    ok = _world_model.db.add_rule(rule_id, condition, conclusion, confidence)
    if ok:
        return {"status": "success", "rule_id": rule_id, "condition": condition, "conclusion": conclusion}
    else:
        return {"status": "error", "message": "Rule ID already exists"}

def world_list_rules() -> Dict:
    rules = _world_model.db.get_rules()
    return {"status": "success", "rules": rules, "count": len(rules)}

def world_delete_rule(rule_id: str) -> Dict:
    _world_model.db.delete_rule(rule_id)
    return {"status": "deleted", "rule_id": rule_id}

def world_run_inference() -> Dict:
    predictions = _world_model.run_forward_chaining()
    return {"status": "success", "new_predictions": predictions, "count": len(predictions)}

def world_get_predictions(limit: int = 50) -> Dict:
    preds = _world_model.get_predictions(limit)
    return {"status": "success", "predictions": preds, "count": len(preds)}

server = BaseMCPServer("world-model", "1.0")

server.register_tool("world_add_rule", {
    "description": "Добавить правило вывода: если условие истинно, то делаем вывод",
    "inputSchema": {
        "type": "object",
        "properties": {
            "condition": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["fact", "entity_exists", "relation_exists"]},
                    "statement": {"type": "string"},
                    "name": {"type": "string"},
                    "source": {"type": "string"},
                    "relation": {"type": "string"},
                    "target": {"type": "string"}
                },
                "additionalProperties": False
            },
            "conclusion": {"type": "string"},
            "confidence": {"type": "number", "default": 0.7},
            "rule_id": {"type": "string"}
        },
        "required": ["condition", "conclusion"]
    }
}, lambda **kw: world_add_rule(kw["condition"], kw["conclusion"], kw.get("confidence", 0.7), kw.get("rule_id")))

server.register_tool("world_list_rules", {
    "description": "Список всех правил",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: world_list_rules())

server.register_tool("world_delete_rule", {
    "description": "Удалить правило по ID",
    "inputSchema": {"type": "object", "properties": {"rule_id": {"type": "string"}}, "required": ["rule_id"]}
}, lambda **kw: world_delete_rule(kw["rule_id"]))

server.register_tool("world_run_inference", {
    "description": "Запустить прямой вывод по всем правилам, сгенерировать новые предсказания",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: world_run_inference())

server.register_tool("world_get_predictions", {
    "description": "Получить активные предсказания",
    "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}}
}, lambda **kw: world_get_predictions(kw.get("limit", 50)))

__mcp_plugin__ = {
    "name": "world-model",
    "version": "1.0",
    "description": "Правила и предсказания для модели мира",
    "dependencies": [],
    "on_load": lambda: _log("[WorldModel] v1.0 loaded")
}

if __name__ == "__main__":
    server.run()