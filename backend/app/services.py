from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from bson import ObjectId
from gridfs import GridFSBucket
from gridfs.errors import NoFile
from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.models import AgentId, BrandKit, ImageResult, StoreCopy, WorkflowEvent


AGENT_ORDER: tuple[AgentId, ...] = (
    "coordinator",
    "branding",
    "copy",
    "image",
    "landing_page",
    "app",
)

AGENT_NAMES: dict[AgentId, str] = {
    "coordinator": "Coordinator",
    "branding": "Branding",
    "copy": "Copy",
    "image": "Image",
    "landing_page": "Landing Page",
    "app": "App",
}

SUPPORTED_IMAGES = {
    "image/jpeg": (".jpg", lambda content: content.startswith(b"\xff\xd8\xff")),
    "image/png": (".png", lambda content: content.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": (
        ".webp",
        lambda content: len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP",
    ),
}


class IdempotencyConflict(Exception):
    pass


class JobNotFound(Exception):
    pass


def detect_image_type(content: bytes, declared_type: str | None = None) -> tuple[str, str]:
    if not content:
        raise ValueError("An uploaded image is empty.")
    for content_type, (extension, matches) in SUPPORTED_IMAGES.items():
        if matches(content):
            if declared_type and declared_type not in {"application/octet-stream", content_type}:
                raise ValueError("The uploaded image content does not match its declared type.")
            return content_type, extension
    raise ValueError("Use a JPEG, PNG, or WebP image.")


def _request_fingerprint(description: str, images: Sequence[tuple[bytes, str]]) -> str:
    source = {
        "description": description,
        "images": [
            {"sha256": sha256(content).hexdigest(), "content_type": content_type}
            for content, content_type in images
        ],
    }
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _initial_agents(now: datetime) -> dict[str, dict[str, Any]]:
    return {
        agent: {
            "name": AGENT_NAMES[agent],
            "status": "waiting",
            "message": "Waiting to start",
            "updated_at": now,
        }
        for agent in AGENT_ORDER
    }


def create_launch_job(
    database,
    bucket: GridFSBucket,
    description: str,
    images: Sequence[tuple[bytes, str]],
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    fingerprint = _request_fingerprint(description, images)
    existing = database.launch_jobs.find_one({"idempotency_key": idempotency_key})
    if existing:
        if existing.get("request_fingerprint") != fingerprint:
            raise IdempotencyConflict
        return existing, False

    job_id = str(uuid4())
    uploaded_ids: list[ObjectId] = []
    image_refs: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    job_inserted = False
    try:
        for content, declared_type in images:
            content_type, extension = detect_image_type(content, declared_type)
            asset_id = bucket.upload_from_stream(
                f"{uuid4()}{extension}",
                BytesIO(content),
                metadata={
                    "job_id": job_id,
                    "content_type": content_type,
                    "sha256": sha256(content).hexdigest(),
                },
            )
            uploaded_ids.append(asset_id)
            image_refs.append(
                {
                    "asset_id": str(asset_id),
                    "content_type": content_type,
                    "size_bytes": len(content),
                    "sha256": sha256(content).hexdigest(),
                }
            )

        job = {
            "_id": job_id,
            "description": description,
            "images": image_refs,
            "status": "queued",
            "current_stage": "coordinator",
            "agents": _initial_agents(now),
            "results": {
                "brand": None,
                "copy": None,
                "images": None,
                "landing_url": None,
                "app": None,
                "summary": None,
            },
            "error": None,
            "workflow_dispatch": "pending",
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "event_seq": 1,
            "created_at": now,
            "updated_at": now,
        }
        try:
            database.launch_jobs.insert_one(job)
            job_inserted = True
        except DuplicateKeyError as exc:
            for asset_id in uploaded_ids:
                bucket.delete(asset_id)
            existing = database.launch_jobs.find_one({"idempotency_key": idempotency_key})
            if existing and existing.get("request_fingerprint") == fingerprint:
                return existing, False
            raise IdempotencyConflict from exc

        try:
            database.job_events.insert_one(
                {
                    "_id": str(uuid4()),
                    "job_id": job_id,
                    "sequence": 1,
                    "type": "job.queued",
                    "agent": None,
                    "to_agent": None,
                    "message": "Launch accepted",
                    "data": {},
                    "occurred_at": now,
                }
            )
        except Exception:
            database.launch_jobs.delete_one({"_id": job_id})
            database.job_events.delete_many({"job_id": job_id})
            job_inserted = False
            raise
        return job, True
    except IdempotencyConflict:
        raise
    except Exception:
        if job_inserted:
            database.launch_jobs.delete_one({"_id": job_id})
            database.job_events.delete_many({"job_id": job_id})
        for asset_id in uploaded_ids:
            bucket.delete(asset_id)
        raise


def _validate_landing_url(value: Any, allowed_hosts: Sequence[str]) -> str:
    if not isinstance(value, str):
        raise ValueError("The landing page URL must be a string.")
    normalized = value.strip()
    parts = urlsplit(normalized)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("The landing page URL must use HTTPS.")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("The landing page URL has an invalid port.") from exc
    if allowed_hosts and parts.hostname.lower() not in allowed_hosts:
        raise ValueError("The landing page URL host is not approved.")
    return normalized


def _normalize_event_data(
    event: WorkflowEvent,
    allowed_hosts: Sequence[str],
) -> dict[str, Any]:
    data = dict(event.data)
    if event.type.startswith("agent.") and event.agent is None:
        raise ValueError("Agent events must include an agent id.")
    if event.type == "agent.handoff" and event.to_agent is None:
        raise ValueError("Handoff events must include a recipient agent id.")

    if event.type == "agent.completed" and event.agent:
        output = data.get("output")
        try:
            if event.agent == "branding":
                data["output"] = BrandKit.model_validate(output).model_dump(mode="json")
            elif event.agent == "copy":
                data["output"] = StoreCopy.model_validate(output).model_dump(mode="json")
            elif event.agent == "image":
                if not isinstance(output, list) or not 1 <= len(output) <= 10:
                    raise ValueError("Image agent output must contain between 1 and 10 images.")
                data["output"] = [
                    ImageResult.model_validate(item).model_dump(mode="json") for item in output
                ]
            elif event.agent == "landing_page":
                raise ValueError("Use landing_page.deployed for the landing page result.")
        except ValidationError as exc:
            raise ValueError(f"{event.agent} output does not match the backend schema.") from exc

    if event.type == "landing_page.deployed":
        data["landing_url"] = _validate_landing_url(
            data.get("landing_url"),
            allowed_hosts,
        )

    if event.type == "job.completed" and data.get("summary") is not None:
        if not isinstance(data["summary"], str) or len(data["summary"]) > 4000:
            raise ValueError("The final summary must be a string of at most 4000 characters.")

    return data


def _updates_for_event(
    event: WorkflowEvent,
    data: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    updates: dict[str, Any] = {"updated_at": now}
    if event.type in {"job.started", "agent.started"}:
        updates["status"] = "running"
    elif event.type == "job.completed":
        updates["status"] = "succeeded"
        updates["current_stage"] = "complete"
        if data.get("summary"):
            updates["results.summary"] = data["summary"]
    elif event.type == "job.failed":
        updates["status"] = "failed"
        updates["error"] = event.message or "The workflow failed."

    if event.agent:
        agent_path = f"agents.{event.agent}"
        if event.type == "agent.started":
            updates[f"{agent_path}.status"] = "working"
        elif event.type == "agent.completed":
            updates[f"{agent_path}.status"] = "done"
        elif event.type == "agent.failed":
            updates[f"{agent_path}.status"] = "failed"
        if event.message:
            updates[f"{agent_path}.message"] = event.message
        updates[f"{agent_path}.updated_at"] = now
        if event.type in {"agent.started", "agent.completed", "agent.failed"}:
            updates["current_stage"] = event.agent

        output = data.get("output")
        result_key = {
            "branding": "brand",
            "copy": "copy",
            "image": "images",
            "app": "app",
        }.get(event.agent)
        if event.type == "agent.completed" and result_key and output is not None:
            updates[f"results.{result_key}"] = output
        if event.agent == "coordinator" and event.type == "agent.completed":
            if isinstance(output, dict) and output.get("summary"):
                updates["results.summary"] = output["summary"]

    if event.type == "agent.handoff" and event.to_agent and event.agent:
        recipient_path = f"agents.{event.to_agent}"
        updates[f"{recipient_path}.message"] = (
            event.message or f"Work received from {AGENT_NAMES[event.agent]}"
        )
        updates[f"{recipient_path}.updated_at"] = now

    if event.type == "landing_page.deployed":
        updates["results.landing_url"] = data["landing_url"]
        updates["current_stage"] = "landing_page"
        updates["agents.landing_page.status"] = "done"
        updates["agents.landing_page.message"] = event.message or "Landing page deployed"
        updates["agents.landing_page.updated_at"] = now

    return updates


def record_workflow_event(
    database,
    job_id: str,
    event: WorkflowEvent,
    allowed_hosts: Sequence[str],
) -> tuple[int, bool]:
    existing_event = database.job_events.find_one({"_id": str(event.event_id)})
    if existing_event:
        if existing_event.get("job_id") != job_id:
            raise ValueError("The event id is already associated with another job.")
        return int(existing_event["sequence"]), True

    data = _normalize_event_data(event, allowed_hosts)
    if event.type == "job.completed":
        job_before_completion = database.launch_jobs.find_one({"_id": job_id})
        if not job_before_completion:
            raise JobNotFound
        results = job_before_completion.get("results", {})
        required_results = ("brand", "copy", "images", "landing_url")
        if any(not results.get(result_key) for result_key in required_results):
            raise ValueError("The job cannot complete until brand, copy, images, and landing URL exist.")
    now = datetime.now(timezone.utc)
    updates = _updates_for_event(event, data, now)
    job = database.launch_jobs.find_one_and_update(
        {"_id": job_id},
        {"$inc": {"event_seq": 1}, "$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        raise JobNotFound
    sequence = int(job["event_seq"])
    event_doc = {
        "_id": str(event.event_id),
        "job_id": job_id,
        "sequence": sequence,
        "type": event.type,
        "agent": event.agent,
        "to_agent": event.to_agent,
        "message": event.message,
        "data": data,
        "occurred_at": now,
        "workflow_occurred_at": event.occurred_at,
    }

    try:
        database.job_events.insert_one(event_doc)
    except DuplicateKeyError:
        existing_event = database.job_events.find_one({"_id": str(event.event_id)})
        if existing_event and existing_event.get("job_id") == job_id:
            return int(existing_event["sequence"]), True
        raise
    return sequence, False


def mark_workflow_dispatch(database, job_id: str, state: str) -> None:
    updates: dict[str, Any] = {
        "workflow_dispatch": state,
        "updated_at": datetime.now(timezone.utc),
    }
    if state == "pending":
        updates.update(
            {
                "status": "queued",
                "current_stage": "coordinator",
                "error": None,
            }
        )
    database.launch_jobs.update_one(
        {"_id": job_id},
        {"$set": updates},
    )


def get_job(database, job_id: str) -> dict[str, Any] | None:
    return database.launch_jobs.find_one({"_id": job_id})


def get_public_status(job: dict[str, Any]) -> dict[str, Any]:
    agents = job.get("agents", {})
    return {
        "job_id": job["_id"],
        "status": job["status"],
        "current_stage": job.get("current_stage"),
        "agents": [
            {
                "id": agent_id,
                "name": AGENT_NAMES[agent_id],
                "status": agents.get(agent_id, {}).get("status", "waiting"),
                "message": agents.get(agent_id, {}).get("message", "Waiting to start"),
                "updated_at": agents.get(agent_id, {}).get("updated_at", job["created_at"]),
            }
            for agent_id in AGENT_ORDER
        ],
        "results": job.get("results", {}),
        "error": job.get("error"),
        "updated_at": job["updated_at"],
    }


def get_events(database, job_id: str, after_sequence: int, limit: int) -> list[dict[str, Any]]:
    cursor = (
        database.job_events.find(
            {"job_id": job_id, "sequence": {"$gt": after_sequence}},
            {"workflow_occurred_at": 0},
        )
        .sort("sequence", 1)
        .limit(limit)
    )
    return [
        {
            "event_id": document["_id"],
            "sequence": document["sequence"],
            "type": document["type"],
            "agent": document.get("agent"),
            "to_agent": document.get("to_agent"),
            "message": document.get("message"),
            "data": document.get("data", {}),
            "occurred_at": document["occurred_at"],
        }
        for document in cursor
    ]


def read_asset(bucket: GridFSBucket, asset_id: str, max_bytes: int) -> tuple[bytes, str]:
    if not ObjectId.is_valid(asset_id):
        raise NoFile("Asset not found")
    stream = bucket.open_download_stream(ObjectId(asset_id))
    if stream.length > max_bytes:
        raise ValueError("Asset is larger than the configured download limit.")
    metadata = stream.metadata or {}
    return stream.read(max_bytes + 1), metadata.get("content_type", "application/octet-stream")
