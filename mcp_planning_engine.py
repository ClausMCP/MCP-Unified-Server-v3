#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Planning Engine v1.0
Планировщик, связывающий Goal Manager, Memory Graph, World Model и Task Manager.
Строит последовательность действий для достижения цели, отслеживает выполнение.
"""

import os
import json
import time
import sqlite3
import threading
import hashlib
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict
from contextlib import contextmanager

from mcp_shared import _log, BaseMCPServer, dialog_ctx

try:
    from mcp_goal_manager import GoalsDB, GoalStatus
    GOAL_MANAGER_AVAILABLE = True
except ImportError:
    GOAL_MANAGER_AVAILABLE = False

try:
    from mcp_memory_graph import _graph_db
    GRAPH_AVAILABLE = True
except ImportError:
    GRAPH_AVAILABLE = False

try:
    from mcp_world_model import WorldModel, WorldModelDB
    WORLD_MODEL_AVAILABLE = True
except ImportError:
    WORLD_MODEL_AVAILABLE = False

try:
    from mcp_task_manager import executor, submit_task, task_status
    TASK_MANAGER_AVAILABLE = True
except ImportError:
    TASK_MANAGER_AVAILABLE = False

PLANNING_DB = os.environ.get("MCP_PLANNING_DB", os.path.join(os.path.dirname(__file__), "planning.db"))

class PlanningDB:
    def __init__(self, db_path: str = PLANNING_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    name TEXT,
                    steps_json TEXT NOT NULL,
                    current_step INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plan_steps (
                    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_args TEXT,
                    status TEXT DEFAULT 'pending',
                    task_id TEXT,
                    started_at REAL,
                    finished_at REAL,
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_goal ON plans(goal_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status)")
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

    def create_plan(self, plan_id: str, goal_id: str, name: str, steps: List[Dict]) -> bool:
        now = time.time()
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO plans (plan_id, goal_id, name, steps_json, current_step, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, 'pending', ?, ?)
                """, (plan_id, goal_id, name, json.dumps(steps), now, now))
                for idx, step in enumerate(steps):
                    conn.execute("""
                        INSERT INTO plan_steps (plan_id, step_index, tool_name, tool_args, status)
                        VALUES (?, ?, ?, ?, 'pending')
                    """, (plan_id, idx, step["tool_name"], json.dumps(step.get("args", {}))))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_plan(self, plan_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if not plan:
                return None
            steps = conn.execute("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_index", (plan_id,)).fetchall()
            result = dict(plan)
            result["steps"] = [dict(s) for s in steps]
            return result

    def update_plan_status(self, plan_id: str, status: str):
        with self._get_conn() as conn:
            conn.execute("UPDATE plans SET status = ?, updated_at = ? WHERE plan_id = ?", (status, time.time(), plan_id))
            conn.commit()

    def update_step(self, plan_id: str, step_index: int, status: str = None, task_id: str = None):
        updates = []
        params = []
        if status:
            updates.append("status = ?")
            params.append(status)
        if task_id:
            updates.append("task_id = ?")
            params.append(task_id)
        if not updates:
            return
        updates.append("finished_at = ?")
        params.append(time.time() if status in ("completed", "failed") else None)
        params.append(plan_id)
        params.append(step_index)
        with self._get_conn() as conn:
            conn.execute(f"UPDATE plan_steps SET {', '.join(updates)} WHERE plan_id = ? AND step_index = ?", params)
            conn.commit()

    def get_pending_plans(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM plans WHERE status = 'pending' OR status = 'in_progress'").fetchall()
            return [dict(r) for r in rows]

class ActionPlanner:
    def __init__(self):
        self.db = PlanningDB()
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="planning_engine")
        self._thread.start()
        _log("[PlanningEngine] Started")

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[PlanningEngine] Stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._process_plans()
            except Exception as e:
                _log(f"[PlanningEngine] Error: {e}")
            self._stop_event.wait(5)

    def _process_plans(self):
        plans = self.db.get_pending_plans()
        for plan in plans:
            if plan["status"] == "pending":
                self._start_plan(plan["plan_id"])
            elif plan["status"] == "in_progress":
                self._advance_plan(plan["plan_id"])

    def _start_plan(self, plan_id: str):
        plan = self.db.get_plan(plan_id)
        if not plan:
            return
        _log(f"[PlanningEngine] Starting plan {plan_id} for goal {plan['goal_id']}")
        self.db.update_plan_status(plan_id, "in_progress")
        self._advance_plan(plan_id)

    def _advance_plan(self, plan_id: str):
        plan = self.db.get_plan(plan_id)
        if not plan:
            return
        steps = plan["steps"]
        current_idx = plan["current_step"]
        while current_idx < len(steps):
            step = steps[current_idx]
            if step["status"] == "pending":
                self._execute_step(plan_id, current_idx, step)
                break
            elif step["status"] == "completed":
                current_idx += 1
                continue
            elif step["status"] == "failed":
                _log(f"[PlanningEngine] Step {current_idx} failed, plan {plan_id} aborted")
                self.db.update_plan_status(plan_id, "failed")
                if GOAL_MANAGER_AVAILABLE:
                    GoalsDB().update_goal(plan["goal_id"], status=GoalStatus.FAILED.value)
                return
            else:
                current_idx += 1
        else:
            self.db.update_plan_status(plan_id, "completed")
            if GOAL_MANAGER_AVAILABLE:
                GoalsDB().update_goal(plan["goal_id"], status=GoalStatus.COMPLETED.value, completed_at=time.time())
            _log(f"[PlanningEngine] Plan {plan_id} completed")

    def _execute_step(self, plan_id: str, step_idx: int, step: Dict):
        tool_name = step["tool_name"]
        tool_args = json.loads(step["tool_args"]) if step["tool_args"] else {}
        _log(f"[PlanningEngine] Executing step {step_idx}: {tool_name}({tool_args})")

        if not TASK_MANAGER_AVAILABLE:
            _log("[PlanningEngine] TaskManager not available, cannot execute step")
            self.db.update_step(plan_id, step_idx, status="failed")
            return

        result = submit_task(tool_name, tool_args, dialog_id="planning_engine")
        if result.get("task_id"):
            task_id = result["task_id"]
            self.db.update_step(plan_id, step_idx, status="running", task_id=task_id)
        else:
            _log(f"[PlanningEngine] Failed to submit task: {result}")
            self.db.update_step(plan_id, step_idx, status="failed")

    def create_plan_for_goal(self, goal_id: str) -> Optional[str]:
        if not GOAL_MANAGER_AVAILABLE:
            _log("[PlanningEngine] GoalManager not available")
            return None
        goals_db = GoalsDB()
        goal = goals_db.get_goal(goal_id)
        if not goal:
            return None
        goal_type = goal["goal_type"]
        metadata = json.loads(goal["metadata"]) if goal["metadata"] else {}
        steps = []
        if goal_type == "atomic":
            tool_name = metadata.get("tool_name")
            tool_args = metadata.get("tool_args", {})
            if tool_name:
                steps.append({"tool_name": tool_name, "args": tool_args})
        elif goal_type == "composite":
            subgoals = goals_db.get_subgoals(goal_id)
            for sg in subgoals:
                sg_meta = json.loads(sg["metadata"]) if sg["metadata"] else {}
                sg_tool = sg_meta.get("tool_name")
                sg_args = sg_meta.get("tool_args", {})
                if sg_tool:
                    steps.append({"tool_name": sg_tool, "args": sg_args})
        else:
            return None

        if not steps:
            _log(f"[PlanningEngine] No steps generated for goal {goal_id}")
            return None

        plan_id = hashlib.md5(f"{goal_id}_{time.time()}".encode()).hexdigest()[:12]
        ok = self.db.create_plan(plan_id, goal_id, f"Plan for {goal['title']}", steps)
        if ok:
            _log(f"[PlanningEngine] Created plan {plan_id} for goal {goal_id}")
            return plan_id
        return None

_planner = ActionPlanner()
_planner.start()

def planning_create_plan(goal_id: str) -> Dict:
    plan_id = _planner.create_plan_for_goal(goal_id)
    if plan_id:
        return {"status": "success", "plan_id": plan_id, "goal_id": goal_id}
    else:
        return {"status": "error", "message": "Could not create plan for goal"}

def planning_get_plan(plan_id: str) -> Dict:
    plan = _planner.db.get_plan(plan_id)
    if plan:
        return {"status": "success", "plan": plan}
    else:
        return {"status": "error", "message": "Plan not found"}

def planning_list_plans(status: str = None) -> Dict:
    plans = _planner.db.get_pending_plans()
    if status:
        plans = [p for p in plans if p["status"] == status]
    return {"status": "success", "plans": plans, "count": len(plans)}

def planning_abort_plan(plan_id: str) -> Dict:
    plan = _planner.db.get_plan(plan_id)
    if not plan:
        return {"status": "error", "message": "Plan not found"}
    _planner.db.update_plan_status(plan_id, "cancelled")
    return {"status": "cancelled", "plan_id": plan_id}

server = BaseMCPServer("planning-engine", "1.0")

server.register_tool("planning_create_plan", {
    "description": "Создать план для достижения цели (по её ID)",
    "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "string"}}, "required": ["goal_id"]}
}, lambda **kw: planning_create_plan(kw["goal_id"]))

server.register_tool("planning_get_plan", {
    "description": "Получить детали плана по ID",
    "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}}, "required": ["plan_id"]}
}, lambda **kw: planning_get_plan(kw["plan_id"]))

server.register_tool("planning_list_plans", {
    "description": "Список активных планов (pending/in_progress)",
    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}
}, lambda **kw: planning_list_plans(kw.get("status")))

server.register_tool("planning_abort_plan", {
    "description": "Отменить выполнение плана",
    "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}}, "required": ["plan_id"]}
}, lambda **kw: planning_abort_plan(kw["plan_id"]))

__mcp_plugin__ = {
    "name": "planning-engine",
    "version": "1.0",
    "description": "Планировщик действий для достижения целей",
    "dependencies": [],
    "on_load": lambda: _log("[PlanningEngine] v1.0 loaded"),
    "on_unload": lambda: _planner.stop()
}

if __name__ == "__main__":
    server.run()