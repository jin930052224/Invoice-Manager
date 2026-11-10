import asyncio

from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.routers import health, invoices, settings as settings_router
from app.routers import auth as auth_router
from app.routers import dingtalk_import
from app.rate_limit import limiter
from app.auth import get_current_user

settings = get_settings()

app = FastAPI(
    title="发票管理系统",
    description="Invoice Manager API - 发票上传、解析、管理系统",
    version="1.0.0",
)

# Add rate limiter to app state
app.state.limiter = limiter

# Custom rate limit exceeded handler with Chinese message
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": f"请求过于频繁，请稍后再试。限制: {exc.detail}"}
    )

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:15173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, prefix="/api", tags=["Health"])
app.include_router(auth_router.router, prefix="/api/auth", tags=["Auth"])
app.include_router(
    invoices.router,
    prefix="/api/invoices",
    tags=["Invoices"],
    dependencies=[Depends(get_current_user)],
)
app.include_router(dingtalk_import.router, prefix="/api/dingtalk-import", tags=["DingTalk Import"], dependencies=[Depends(get_current_user)])
app.include_router(
    settings_router.router,
    prefix="/api/settings",
    tags=["Settings"],
    dependencies=[Depends(get_current_user)],
)


@app.on_event("startup")
async def startup():
    from app.database import engine, Base, async_session_maker
    from app.models.user import User  # noqa: ensure table is registered
    from app.auth import hash_password
    from sqlalchemy import select, text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 迁移：为已有数据库添加 uploaded_by_id 列（幂等操作）
        await conn.execute(text(
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS uploaded_by_id INTEGER REFERENCES users(id)"
        ))
        await conn.execute(text(
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS approval_instance_id VARCHAR(100)"
        ))
        await conn.execute(text(
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS document_type VARCHAR(20) NOT NULL DEFAULT 'invoice'"
        ))
        await conn.execute(text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS approval_claim_amount NUMERIC(12, 2)"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_invoices_approval_instance_id ON invoices (approval_instance_id)"
        ))

    # Seed default admin user if no users exist
    async with async_session_maker() as session:
        result = await session.execute(select(User).limit(1))
        if not result.scalar_one_or_none():
            admin = User(
                username="admin",
                display_name="管理员",
                hashed_password=hash_password("qwe123"),
                is_admin=True,
            )
            session.add(admin)
            await session.commit()

    # Recover uploads that were queued in memory when the backend last stopped.
    # This task only queues durable UPLOADED/PROCESSING rows; parsing itself is
    # concurrency-limited by the invoice router.
    asyncio.create_task(invoices.resume_interrupted_processing())


@app.get("/")
async def root():
    return {"message": "发票管理系统 API", "version": "1.0.0"}
