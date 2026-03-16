import httpx
import os
from dotenv import load_dotenv

load_dotenv()
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")


async def fetch_rates(source: str = "cbr") -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/fx/rates",
                params={"source": source}
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"Ошибка запроса к backend: {e}")
        return None


async def fetch_sources() -> list | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BACKEND_URL}/api/fx/sources")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"Ошибка запроса к backend: {e}")
        return None
