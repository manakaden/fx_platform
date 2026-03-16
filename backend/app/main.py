from fastapi import FastAPI
from app.api.fx import router as fx_router

app = FastAPI(title="FX Platform API", version="0.1.0")

@app.get("/health")
async def health():
    return {"status": "ok"}

app.include_router(fx_router, prefix="/api/fx", tags=["fx"])
