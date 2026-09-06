from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scalar_fastapi import get_scalar_api_reference
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.core.config import settings
from src.routes.agent_runs import agent_runs_router
from src.routes.auth import auth_router, me_router
from src.routes.dashboard import dashboard_router
from src.routes.feedback import feedback_router
from src.routes.github import github_router
from src.routes.repositories import router as repositories_router
from src.routes.tasks import tasks_router
from src.routes.ws import ws_router

app = FastAPI(
    title="P.A.T.C.H.",
    description="AI Coding Agent MVP",
    version="0.1.0",
    docs_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # ty: ignore[invalid-argument-type]

app.include_router(auth_router)
app.include_router(me_router)
app.include_router(github_router)
app.include_router(repositories_router)
app.include_router(tasks_router)
app.include_router(agent_runs_router)
app.include_router(feedback_router)
app.include_router(dashboard_router)
app.include_router(ws_router)


@app.get("/")
def read_root():
    return {"message": "Halo kids"}


@app.get("/scalar")
def get_scalar():
    return get_scalar_api_reference(openapi_url=app.openapi_url, title=app.title)


@app.get("/health")
def health_check():
    return {"status": "ok"}
