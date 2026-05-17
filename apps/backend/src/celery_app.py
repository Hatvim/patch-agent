from celery import Celery

from src.core.config import settings

celery_app = Celery(
    "patch",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "src.services.agent_runner",
        "src.services.review_runner",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
