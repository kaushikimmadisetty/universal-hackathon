from gridfs import GridFSBucket
from pymongo import ASCENDING, MongoClient

from app.config import Settings


def connect(settings: Settings):
    if not settings.mongodb_uri:
        return None, None, None

    client = MongoClient(
        settings.mongodb_uri,
        connectTimeoutMS=5000,
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
    )
    client.admin.command("ping")
    database = client[settings.database_name]
    database.launch_jobs.create_index("idempotency_key", unique=True, name="idempotency_key")
    database.job_events.create_index(
        [("job_id", ASCENDING), ("sequence", ASCENDING)],
        unique=True,
        name="job_event_sequence",
    )
    bucket = GridFSBucket(database, bucket_name=settings.gridfs_bucket)
    return client, database, bucket
