"""Persistence for habit-change plans."""

from __future__ import annotations

from typing import Any, Optional


class SimulationsRepoMixin:
    def habit_change_plans(self, active_only: bool = False) -> list[Any]:
        where = "WHERE p.status='active'" if active_only else ""
        return self.q(
            f"""
            SELECT p.*, IFNULL(g.name, '') AS goal_name
            FROM habit_change_plans p
            LEFT JOIN goals g ON g.id=p.goal_id
            {where}
            ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                     p.start_date DESC, p.id DESC
            """
        )

    def habit_change_plan_by_id(self, plan_id: int) -> Optional[Any]:
        rows = self.q(
            """
            SELECT p.*, IFNULL(g.name, '') AS goal_name
            FROM habit_change_plans p
            LEFT JOIN goals g ON g.id=p.goal_id
            WHERE p.id=?
            """,
            (int(plan_id),),
        )
        return rows[0] if rows else None

    def add_habit_change_plan(
        self,
        *,
        name: str,
        preset_key: str,
        habit_name: str,
        scenario_key: str,
        baseline_unit_cost: float,
        baseline_frequency: float,
        frequency_unit: str,
        alternative_unit_cost: float,
        target_frequency: float,
        baseline_monthly_cost: float,
        planned_monthly_cost: float,
        planned_monthly_savings: float,
        goal_id: Optional[int],
        start_date: str,
        audit_rules_json: str = "{}",
        notes: str = "",
    ) -> int:
        try:
            cursor = self.db.conn.execute(
                """
                INSERT INTO habit_change_plans(
                    name,preset_key,habit_name,scenario_key,baseline_unit_cost,
                    baseline_frequency,frequency_unit,alternative_unit_cost,
                    target_frequency,baseline_monthly_cost,planned_monthly_cost,
                    planned_monthly_savings,goal_id,start_date,status,
                    audit_rules_json,notes
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)
                """,
                (
                    str(name or "").strip() or f"Plano {habit_name}",
                    str(preset_key or "").strip(),
                    str(habit_name or "").strip(),
                    str(scenario_key or "").strip(),
                    max(0.0, float(baseline_unit_cost or 0.0)),
                    max(0.0, float(baseline_frequency or 0.0)),
                    str(frequency_unit or "month"),
                    max(0.0, float(alternative_unit_cost or 0.0)),
                    max(0.0, float(target_frequency or 0.0)),
                    max(0.0, float(baseline_monthly_cost or 0.0)),
                    max(0.0, float(planned_monthly_cost or 0.0)),
                    max(0.0, float(planned_monthly_savings or 0.0)),
                    int(goal_id) if goal_id else None,
                    str(start_date or "")[:10],
                    str(audit_rules_json or "{}"),
                    str(notes or "").strip(),
                ),
            )
            self.db.conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            self.db.conn.rollback()
            raise

    def set_habit_change_plan_status(self, plan_id: int, status: str) -> None:
        selected = str(status or "").strip().lower()
        if selected not in {"active", "paused", "completed"}:
            raise ValueError(f"Invalid habit plan status: {status}")
        try:
            cursor = self.db.conn.execute(
                """
                UPDATE habit_change_plans
                SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (selected, int(plan_id)),
            )
            self.db.conn.commit()
            if cursor.rowcount == 0:
                raise ValueError(f"Habit change plan not found: {plan_id}")
        except Exception:
            self.db.conn.rollback()
            raise

    def delete_habit_change_plan(self, plan_id: int) -> None:
        try:
            self.db.conn.execute(
                "DELETE FROM habit_change_plans WHERE id=?",
                (int(plan_id),),
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise


__all__ = ["SimulationsRepoMixin"]
