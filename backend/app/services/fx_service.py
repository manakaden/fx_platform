import httpx
from typing import Optional

SOURCES = {
    "cbr": {
        "title": "ЦБ РФ",
        "mode": "direct_cbr",
        "url": "https://www.cbr-xml-daily.ru/daily_json.js",
    },
    "exchangerate_api": {
        "title": "ExchangeRate-API",
        "mode": "rub_base_inverted",
        "url": "https://api.exchangerate-api.com/v4/latest/RUB",
    },
    "open_er_api": {
        "title": "open.er-api.com",
        "mode": "rub_base_inverted",
        "url": "https://open.er-api.com/v6/latest/RUB",
    },
}


async def get_rates(source_key: str = "cbr") -> Optional[dict]:
    src = SOURCES.get(source_key)
    if not src:
        return None

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(src["url"])
            data = resp.json()
        except Exception as e:
            print(f"Ошибка запроса к {source_key}: {e}")
            return None

    try:
        if src["mode"] == "direct_cbr":
            usd = float(data["Valute"]["USD"]["Value"])
            eur = float(data["Valute"]["EUR"]["Value"])
            cny = float(data["Valute"]["CNY"]["Value"])

        elif src["mode"] == "rub_base_inverted":
            usd = 1.0 / float(data["rates"]["USD"])
            eur = 1.0 / float(data["rates"]["EUR"])
            cny = 1.0 / float(data["rates"]["CNY"])

        else:
            return None

        return {
            "source_key": source_key,
            "source_title": src["title"],
            "usd_rub": round(usd, 4),
            "eur_rub": round(eur, 4),
            "cny_rub": round(cny, 4),
        }

    except Exception as e:
        print(f"Ошибка обработки данных из {source_key}: {e}")
        return None
