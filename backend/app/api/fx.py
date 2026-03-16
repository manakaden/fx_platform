from fastapi import APIRouter, HTTPException
from app.services.fx_service import get_rates

router = APIRouter()


@router.get("/rates")
async def rates(source: str = "cbr"):
    data = await get_rates(source)
    if data is None:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось получить курсы для источника: {source}"
        )
    return data


@router.get("/sources")
async def sources():
    from app.services.fx_service import SOURCES
    return [
        {"key": k, "title": v["title"]}
        for k, v in SOURCES.items()
    ]
