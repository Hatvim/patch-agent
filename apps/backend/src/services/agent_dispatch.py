import logging
import uuid

logger = logging.getLogger(__name__)

def enqueue_agent_run(agent_run_id: uuid.UUID) -> None:
    """
    Attempt to dispatch the agent run via Celery.

    Stream 3 owns `src.tasks.agent.dispatch_agent_run`.  If that module is
    not yet available (ImportError) or the broker is unreachable, we log a
    warning and continue — the run stays in `queued` status and can be
    retried once Stream 3 is integrated.
    """
    try:
        from src.services.agent_runner import dispatch_agent_run  # noqa: PLC0415

        dispatch_agent_run.delay(str(agent_run_id))
        logger.info("Enqueued dispatch_agent_run for agent run %s.", agent_run_id)
    except ImportError:
        logger.warning(
            "[CELERY STUB] dispatch_agent_run not found — Stream 3 not yet integrated. Agent run %s is queued but will not be dispatched until Stream 3 lands.",
            agent_run_id,
        )
        raise
    except Exception as exc:
        logger.warning(
            "[CELERY STUB] Failed to enqueue dispatch_agent_run for agent run %s: %s. The run is stored in the DB and can be retried.",
            agent_run_id,
            exc,
        )
        raise
