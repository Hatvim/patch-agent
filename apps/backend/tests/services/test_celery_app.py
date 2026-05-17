from src.celery_app import celery_app


def test_celery_app_imports_dispatch_tasks():
    celery_app.loader.import_default_modules()

    assert "dispatch_agent_run" in celery_app.tasks
    assert "dispatch_review_run" in celery_app.tasks
