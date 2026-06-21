"""Shared, blueprint-agnostic helpers for the Inventory Tracker (incremental
refactor — see REFACTOR_PLAN.md). Nothing here imports app.py or any blueprint,
so both the app and blueprints can depend on it without import cycles."""
