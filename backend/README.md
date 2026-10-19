# Backend service

This service is the API and persistence boundary for the launch demo. FastAPI owns launch jobs, photo intake, Atlas state, and the UI status contract. n8n owns the Coordinator and agent workflow execution. The n8n workflows publish their BAND handoffs and send signed progress callbacks to this API.

## Local setup

1. Copy the root .env.example to .env and fill in MongoDB Atlas and n8n values.
2. Set PUBLIC_API_BASE_URL to an address reachable by n8n. localhost works only when n8n runs on the same machine.
3. Install the package from the repository root:

       python -m pip install -e backend

4. Start the API from the repository root:

       uvicorn app.main:app --app-dir backend --env-file .env --reload

The interactive API schema is available at /docs. The liveness endpoint is /healthz and the database readiness endpoint is /readyz.

## Frontend contract

### Start a launch

POST /launch uses multipart/form-data:

- description: required product description, up to 4,000 characters.
- images[]: repeat this field for 1 to 5 JPEG, PNG, or WebP images.
- Idempotency-Key: optional request header. Reusing the same key with the same description and images returns the original job. Reusing it for different input returns 409.

The API returns 202 with job_id, status, status_url, and events_url. Photos are stored in Atlas GridFS. The job document contains only image references and metadata.

### Read progress

GET /status/{job_id} returns status, current_stage, the six agent cards, results, error, and updated_at. The frontend can poll this endpoint every two seconds.

GET /events/{job_id}?after_seq=0&limit=100 returns the ordered event history. Set after_seq to the last sequence already received when polling for new events.

Job status values are queued, running, succeeded, and failed. Agent status values are waiting, working, done, and failed.

### Result schemas

Branding sends this as data.output in its agent.completed event:

    {
      "business_name": "Cobalt & Butter",
      "logo_concept": "A simple mark ...",
      "colors": [
        {"name": "Cobalt", "hex": "#2340E6"},
        {"name": "Butter", "hex": "#FFF1C7"}
      ],
      "fonts": {"heading": "Bricolage Grotesque", "body": "Inter"},
      "tone_of_voice": "Plainspoken and maker-friendly"
    }

Copy sends headline, product_description, about, and cta_label.

Image sends a list of 1 to 10 objects with url, alt, and optional role.

Landing Page sends a landing_page.deployed event with data.landing_url. The URL must use HTTPS. Configure LANDING_PAGE_ALLOWED_HOSTS to restrict it to the selected deployment provider.

The Coordinator can include a summary in data.summary on job.completed or in its completed output.

## n8n contract

FastAPI calls the configured N8N_WEBHOOK_URL with a bearer token. The webhook should acknowledge the launch promptly and continue the run in n8n. Treat job_id as the launch idempotency key so retries do not start duplicate agent runs.

The launch payload has schema_version, job_id, description, images, and callback_url. Each image includes asset_id, content_type, size_bytes, sha256, and fetch_url. The fetch_url is signed, expires after one hour, and lets n8n retrieve the uploaded product photo without making it public.

Send callbacks to callback_url as JSON with these fields:

    {
      "schema_version": 1,
      "event_id": "a UUID generated once and reused on retry",
      "type": "agent.started",
      "agent": "branding",
      "to_agent": null,
      "message": "Creating the brand kit",
      "data": {},
      "occurred_at": "2026-10-19T17:00:00Z"
    }

The API creates the initial job.queued event itself. Supported n8n event types are job.started, job.completed, job.failed, agent.started, agent.message, agent.handoff, agent.completed, agent.failed, and landing_page.deployed. Agent ids are coordinator, branding, copy, image, landing_page, and app.

For each callback, sign the exact raw request body with HMAC-SHA256 using N8N_CALLBACK_SECRET. Use the Unix timestamp and a period as the prefix: timestamp + "." + raw_body. Send the timestamp in X-Webhook-Timestamp and the lowercase digest as sha256=<hex> in X-Webhook-Signature. The API rejects callbacks older than five minutes and deduplicates event_id.

Use agent.handoff for visible BAND handoffs. Include agent as the sender, to_agent as the recipient, and a short message. Keep the workflow payload and callback data free of credentials. The app API is the job-state source of truth; workflows should report events instead of writing directly to Atlas.

## Atlas collections

- launch_jobs stores launch input, image references, status, agent snapshots, results, and dispatch metadata.
- job_events stores the append-only event timeline with a unique per-job sequence.
- launch_assets.files and launch_assets.chunks are created by GridFS for uploaded image bytes.

Atlas access must be configured in MONGODB_URI with a database user limited to this database. Keep .env out of Git and configure a public HTTPS API URL for remote n8n.
