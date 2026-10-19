from contextlib import asynccontextmanager
import logging
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from gridfs.errors import NoFile
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import ValidationError
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.database import connect
from app.models import (
    CallbackAck,
    EventResponse,
    JobStatusResponse,
    LaunchAccepted,
    WorkflowEvent,
)
from app.security import build_asset_url, verify_asset_signature, verify_webhook_signature
from app.services import (
    IdempotencyConflict,
    JobNotFound,
    create_launch_job,
    detect_image_type,
    get_events,
    get_job,
    get_public_status,
    mark_workflow_dispatch,
    read_asset,
    record_workflow_event,
)


logger = logging.getLogger(__name__)
settings = Settings.from_env()
MAX_CALLBACK_BYTES = 128 * 1024


def _usable_secret(value: str | None) -> bool:
    return bool(value and len(value) >= 32 and not value.startswith("replace-with"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    client, database, bucket = connect(settings)
    app.state.mongo_client = client
    app.state.database = database
    app.state.asset_bucket = bucket
    try:
        yield
    finally:
        if client is not None:
            client.close()


app = FastAPI(
    title="Universal Hackathon Backend",
    description="Launch jobs, n8n workflow callbacks, and live job status.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)


def _database(request: Request):
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="MongoDB is not configured.")
    return database


def _asset_bucket(request: Request):
    bucket = getattr(request.app.state, "asset_bucket", None)
    if bucket is None:
        raise HTTPException(status_code=503, detail="MongoDB asset storage is not configured.")
    return bucket


def _launch_response(job_id: str, status: str) -> LaunchAccepted:
    return LaunchAccepted(
        job_id=UUID(job_id),
        status=status,
        status_url=f"/status/{job_id}",
        events_url=f"/events/{job_id}",
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(request: Request):
    database = _database(request)
    try:
        database.command("ping")
    except PyMongoError as exc:
        logger.warning("MongoDB readiness check failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="MongoDB is unavailable.") from exc
    return {
        "status": "ready",
        "database": "connected",
        "workflow_configured": bool(settings.n8n_webhook_url and settings.n8n_webhook_token),
    }


@app.post("/launch", response_model=LaunchAccepted, status_code=202)
async def launch(
    request: Request,
    description: Annotated[str, Form(min_length=1, max_length=4000)],
    images: Annotated[list[UploadFile], File(alias="images[]")],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128),
    ] = None,
):
    database = _database(request)
    bucket = _asset_bucket(request)
    missing_settings = [
        name
        for name, value in (
            ("N8N_WEBHOOK_URL", settings.n8n_webhook_url),
            ("N8N_WEBHOOK_TOKEN", settings.n8n_webhook_token),
        (
            "N8N_CALLBACK_SECRET",
            settings.n8n_callback_secret if _usable_secret(settings.n8n_callback_secret) else None,
        ),
        (
            "ASSET_SIGNING_SECRET",
            settings.asset_signing_secret if _usable_secret(settings.asset_signing_secret) else None,
        ),
            ("PUBLIC_API_BASE_URL", settings.public_api_base_url),
        )
        if not value
    ]
    if missing_settings:
        raise HTTPException(
            status_code=503,
            detail="Workflow integration is missing configuration: " + ", ".join(missing_settings),
        )

    normalized_description = description.strip()
    if not normalized_description:
        raise HTTPException(status_code=422, detail="Description cannot be blank.")
    if not 1 <= len(images) <= 5:
        raise HTTPException(status_code=422, detail="Upload between 1 and 5 product images.")

    key = (idempotency_key or str(uuid4())).strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key cannot be blank.")

    uploaded_images: list[tuple[bytes, str]] = []
    total_bytes = 0
    try:
        for image in images:
            content = await image.read(settings.max_image_bytes + 1)
            if len(content) > settings.max_image_bytes:
                max_mb = max(1, settings.max_image_bytes // (1024 * 1024))
                raise HTTPException(
                    status_code=413,
                    detail=f"Each image must be {max_mb} MB or smaller.",
                )
            total_bytes += len(content)
            if total_bytes > settings.max_total_upload_bytes:
                max_total_mb = max(1, settings.max_total_upload_bytes // (1024 * 1024))
                raise HTTPException(
                    status_code=413,
                    detail=f"The total upload must be {max_total_mb} MB or smaller.",
                )
            try:
                content_type, _ = detect_image_type(content, image.content_type)
            except ValueError as exc:
                raise HTTPException(status_code=415, detail=str(exc)) from exc
            uploaded_images.append((content, content_type))
    finally:
        for image in images:
            await image.close()

    try:
        job, is_new = await run_in_threadpool(
            create_launch_job,
            database,
            bucket,
            normalized_description,
            uploaded_images,
            key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="This Idempotency-Key was already used for a different launch request.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except PyMongoError as exc:
        logger.exception("Could not create launch job")
        raise HTTPException(status_code=503, detail="Could not save the launch job.") from exc

    job_id = job["_id"]
    if not is_new and job.get("workflow_dispatch") == "started":
        return _launch_response(job_id, job["status"])
    if not is_new and job.get("workflow_dispatch") == "failed":
        await run_in_threadpool(mark_workflow_dispatch, database, job_id, "pending")

    callback_url = (
        f"{settings.public_api_base_url}/internal/jobs/{job_id}/events"
    )
    workflow_payload = {
        "schema_version": 1,
        "job_id": job_id,
        "description": normalized_description,
        "images": [
            {
                **image,
                "fetch_url": build_asset_url(
                    settings.public_api_base_url,
                    image["asset_id"],
                    settings.asset_signing_secret,
                ),
            }
            for image in job["images"]
        ],
        "callback_url": callback_url,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.post(
                settings.n8n_webhook_url,
                json=workflow_payload,
                headers={"Authorization": f"Bearer {settings.n8n_webhook_token}"},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("n8n workflow dispatch failed for job %s", job_id)
        failure_event = WorkflowEvent(
            event_id=uuid4(),
            type="job.failed",
            message="The workflow could not be started. Please try launching again.",
        )
        await run_in_threadpool(
            record_workflow_event,
            database,
            job_id,
            failure_event,
            settings.landing_page_allowed_hosts,
        )
        await run_in_threadpool(mark_workflow_dispatch, database, job_id, "failed")
        raise HTTPException(
            status_code=502,
            detail={
                "job_id": job_id,
                "status_url": f"/status/{job_id}",
                "message": "The workflow could not be started; the launch is saved as failed.",
            },
        ) from exc

    await run_in_threadpool(mark_workflow_dispatch, database, job_id, "started")
    current_job = await run_in_threadpool(get_job, database, job_id)
    return _launch_response(job_id, current_job["status"])


@app.get("/status/{job_id}", response_model=JobStatusResponse)
async def status(job_id: UUID, request: Request):
    database = _database(request)
    job = await run_in_threadpool(get_job, database, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Launch job not found.")
    return get_public_status(job)


@app.get("/events/{job_id}", response_model=list[EventResponse])
async def events(
    job_id: UUID,
    request: Request,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    database = _database(request)
    job = await run_in_threadpool(get_job, database, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Launch job not found.")
    return await run_in_threadpool(
        get_events,
        database,
        str(job_id),
        after_seq,
        limit,
    )


@app.post("/internal/jobs/{job_id}/events", response_model=CallbackAck)
async def workflow_event(job_id: UUID, request: Request):
    secret = settings.n8n_callback_secret
    if not _usable_secret(secret):
        raise HTTPException(status_code=503, detail="Workflow callbacks are not configured.")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CALLBACK_BYTES:
            raise HTTPException(status_code=413, detail="Workflow event is too large.")
    raw_body = bytes(body)

    if not verify_webhook_signature(
        raw_body,
        request.headers.get("X-Webhook-Timestamp"),
        request.headers.get("X-Webhook-Signature"),
        secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid workflow event signature.")

    try:
        event = WorkflowEvent.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Workflow event does not match the contract.") from exc

    try:
        sequence, duplicate = await run_in_threadpool(
            record_workflow_event,
            _database(request),
            str(job_id),
            event,
            settings.landing_page_allowed_hosts,
        )
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Launch job not found.") from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PyMongoError as exc:
        logger.exception("Could not persist workflow event for job %s", job_id)
        raise HTTPException(status_code=503, detail="Could not save workflow progress.") from exc

    return CallbackAck(sequence=sequence, duplicate=duplicate)


@app.get("/internal/assets/{asset_id}")
async def fetch_asset(
    asset_id: str,
    request: Request,
    expires: Annotated[int, Query()],
    signature: Annotated[str, Query(min_length=64, max_length=64)],
):
    secret = settings.asset_signing_secret
    if not _usable_secret(secret) or not verify_asset_signature(asset_id, expires, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid or expired asset URL.")

    try:
        content, content_type = await run_in_threadpool(
            read_asset,
            _asset_bucket(request),
            asset_id,
            settings.max_image_bytes,
        )
    except NoFile as exc:
        raise HTTPException(status_code=404, detail="Uploaded image not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except PyMongoError as exc:
        logger.exception("Could not read uploaded image")
        raise HTTPException(status_code=503, detail="Image storage is unavailable.") from exc
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
