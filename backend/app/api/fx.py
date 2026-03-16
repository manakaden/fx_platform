from fastapi import APIRouter

router = APIRouter()

@router.get("/rates")
async def get_rates(source: str = "cbr"):
    # временная заглушка — реальная логика в следующем спринте
    return {
        "source": source,
        "usd_rub": 90.0,
        "eur_rub": 100.0,
        "cny_rub": 12.0,
    }
