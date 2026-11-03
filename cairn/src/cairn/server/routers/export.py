import sqlite3
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
import yaml

from cairn.server.db import get_conn
from cairn.server.models import is_goal_id
from cairn.server.services import (
    derive_project_completion,
    expire_reason_leases,
    expire_workers,
    get_completed_goal_ids,
    get_project_or_404,
)

router = APIRouter(tags=["export"])


@dataclass
class ProjectExportData:
    proj: sqlite3.Row
    facts: list[sqlite3.Row]
    hints: list[sqlite3.Row]
    intents: list[sqlite3.Row]
    sources_by_intent: dict[str, list[str]]
    targets_by_intent: dict[str, list[str]]


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str) -> ProjectExportData:
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    targets_by_intent = {}
    for i in intents:
        sources = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in sources]
        targets = conn.execute(
            "SELECT fact_id FROM intent_targets WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        targets_by_intent[i["id"]] = [r["fact_id"] for r in targets]

    return ProjectExportData(proj, facts, hints, intents, sources_by_intent, targets_by_intent)


def _export_yaml(conn, project_id: str) -> str:
    bundle = _load_project_data(conn, project_id)
    completed_goals = get_completed_goal_ids(conn, project_id)

    origin_desc = ""
    for f in bundle.facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
            break

    data: dict = {
        "project": {
            "title": bundle.proj["title"],
            "origin": origin_desc,
            "bootstrap_enabled": bool(bundle.proj["bootstrap_enabled"]),
        }
    }

    goals = [
        {
            "id": f["id"],
            "description": f["description"],
            "created_at": format_export_timestamp(f["created_at"]),
            "status": "completed" if f["id"] in completed_goals else "open",
        }
        for f in bundle.facts
        if is_goal_id(f["id"])
    ]
    if goals:
        data["goals"] = goals

    if bundle.hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in bundle.hints
        ]

    data["facts"] = [
        {
            "id": f["id"],
            "description": f["description"],
            "created_at": format_export_timestamp(f["created_at"]),
        }
        for f in bundle.facts
        if not is_goal_id(f["id"])
    ]

    intent_list = []
    for i in bundle.intents:
        entry: dict = {
            "from": bundle.sources_by_intent.get(i["id"], []),
            "to": bundle.targets_by_intent.get(i["id"], []),
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    bundle = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in bundle.facts}
    goal_rows = [f for f in bundle.facts if is_goal_id(f["id"])]

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    ts = format_export_timestamp(bundle.proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}"
    events.append((bundle.proj["created_at"] or "", order, block))
    order += 1

    for g in goal_rows:
        ts = format_export_timestamp(g["created_at"]) or ""
        actor = g["creator"] or "system"
        block = f"[{ts}] GOAL DECLARED {g['id']} by {actor}\n  {g['description']}"
        events.append((g["created_at"] or "", order, block))
        order += 1

    for h in bundle.hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    concluded = [
        i
        for i in bundle.intents
        if i["concluded_at"] and bundle.targets_by_intent.get(i["id"])
    ]
    concluded.sort(key=lambda i: (i["concluded_at"] or "", i["id"]))

    for i in bundle.intents:
        ts = format_export_timestamp(i["created_at"]) or ""
        src = bundle.sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

    completion = derive_project_completion(conn, project_id)
    completion_intent = completion.completed_by_intent if completion else None
    for i in concluded:
        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]
        src = bundle.sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)
        targets = bundle.targets_by_intent.get(i["id"], [])
        goal_targets = [t for t in targets if is_goal_id(t)]
        fact_targets = [t for t in targets if not is_goal_id(t)]

        if fact_targets:
            produced = ", ".join(fact_targets)
            fact_desc = facts_by_id.get(fact_targets[0], "")
            block = (
                f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n"
                f"  from: {from_str}\n"
                f"  produced: {produced}\n"
                f"  {fact_desc}"
            )
            events.append((i["concluded_at"] or "", order, block))
            order += 1

        for gid in goal_targets:
            block = (
                f"[{ts}] GOAL COMPLETED {gid} by {actor}\n"
                f"  via: {i['id']} from {from_str}"
            )
            events.append((i["concluded_at"] or "", order, block))
            order += 1

        if i["id"] == completion_intent:
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
            events.append((i["concluded_at"] or "", order, block))
            order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


@router.get("/projects/{project_id}/export")
def export_project(request: Request, project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "Supported formats: yaml, timeline")

    with get_conn(request) as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")
