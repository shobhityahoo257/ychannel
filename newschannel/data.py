"""Official economic data for explainers: World Bank indicators (free, no key).

A data series becomes a Source in the fact ledger - plain sentences such as "In 2022, India's GDP growth was 7.0 percent." -
so every number goes through the same code-verified claim pipeline as an article. The raw series is kept on the source so a
chart can be drawn from it. Nothing here is written by an AI."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

import requests

from .ledger import Source, classify_tier, outlet_of

API = "https://api.worldbank.org/v2"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ExplainerBot/0.1; +research)"}

# key -> (World Bank code, label, unit-kind). kinds: pct (percent), usd (US dollars), people, ratio
INDICATORS: dict[str, tuple[str, str, str]] = {
    "gdp_growth": ("NY.GDP.MKTP.KD.ZG", "GDP growth", "pct"),
    "gdp": ("NY.GDP.MKTP.CD", "GDP (current US$)", "usd"),
    "gdp_per_capita": ("NY.GDP.PCAP.CD", "GDP per capita (current US$)", "usd"),
    "inflation": ("FP.CPI.TOTL.ZG", "Inflation (consumer prices)", "pct"),
    "unemployment": ("SL.UEM.TOTL.ZS", "Unemployment (share of labour force)", "pct"),
    "trade": ("NE.TRD.GNFS.ZS", "Trade (share of GDP)", "pct"),
    "exports": ("NE.EXP.GNFS.ZS", "Exports of goods and services (share of GDP)", "pct"),
    "imports": ("NE.IMP.GNFS.ZS", "Imports of goods and services (share of GDP)", "pct"),
    "fdi": ("BX.KLT.DINV.WD.GD.ZS", "Foreign direct investment, net inflows (share of GDP)", "pct"),
    "current_account": ("BN.CAB.XOKA.GD.ZS", "Current account balance (share of GDP)", "pct"),
    "debt": ("GC.DOD.TOTL.GD.ZS", "Central government debt (share of GDP)", "pct"),
    "reserves": ("FI.RES.TOTL.CD", "Total reserves including gold (current US$)", "usd"),
    "population": ("SP.POP.TOTL", "Population", "people"),
    "savings": ("NY.GNS.ICTR.ZS", "Gross savings (share of GDP)", "pct"),
    "manufacturing": ("NV.IND.MANF.ZS", "Manufacturing value added (share of GDP)", "pct"),
    "gini": ("SI.POV.GINI", "Income inequality (Gini index)", "ratio"),
}

COUNTRIES = {
    "india": "IND", "china": "CHN", "united states": "USA", "usa": "USA", "us": "USA", "america": "USA", "japan": "JPN",
    "germany": "DEU", "united kingdom": "GBR", "uk": "GBR", "britain": "GBR", "france": "FRA", "italy": "ITA", "canada": "CAN",
    "brazil": "BRA", "russia": "RUS", "south korea": "KOR", "korea": "KOR", "australia": "AUS", "mexico": "MEX",
    "indonesia": "IDN", "saudi arabia": "SAU", "turkey": "TUR", "turkiye": "TUR", "argentina": "ARG", "south africa": "ZAF",
    "nigeria": "NGA", "egypt": "EGY", "pakistan": "PAK", "bangladesh": "BGD", "sri lanka": "LKA", "nepal": "NPL",
    "vietnam": "VNM", "thailand": "THA", "malaysia": "MYS", "singapore": "SGP", "philippines": "PHL", "iran": "IRN",
    "israel": "ISR", "ukraine": "UKR", "poland": "POL", "spain": "ESP", "netherlands": "NLD", "switzerland": "CHE",
    "united arab emirates": "ARE", "uae": "ARE", "world": "WLD", "euro area": "EMU", "european union": "EUU",
}


class DataError(RuntimeError):
    pass


def _get(url: str, params: dict[str, Any] | None = None, http: Callable | None = None) -> Any:
    r = (http or requests.get)(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def resolve_country(name: str, http: Callable | None = None) -> tuple[str, str]:
    """(ISO3 code, display name). Built-in names first, then the World Bank country list."""
    raw = (name or "").strip()
    if not raw:
        raise DataError("Give a country name.")
    key = raw.lower()
    if key in COUNTRIES:
        return COUNTRIES[key], _nice(key)
    if re.fullmatch(r"[A-Za-z]{3}", raw):
        return raw.upper(), raw.upper()
    try:
        page = _get(f"{API}/country", {"format": "json", "per_page": 400}, http)
        for c in page[1]:
            if key in (c.get("name", "").lower(), c.get("iso2Code", "").lower(), c.get("id", "").lower()):
                return c["id"], c["name"]
    except Exception as exc:
        raise DataError(f"Could not look up '{raw}': {exc}") from exc
    raise DataError(f"Unknown country '{raw}'. Try its English name or 3-letter code (e.g. IND).")


def _nice(key: str) -> str:
    small = {"usa": "the United States", "us": "the United States", "america": "the United States", "uk": "the United Kingdom",
             "britain": "the United Kingdom", "uae": "the United Arab Emirates", "united states": "the United States",
             "united kingdom": "the United Kingdom", "united arab emirates": "the United Arab Emirates",
             "netherlands": "the Netherlands", "philippines": "the Philippines", "euro area": "the euro area",
             "european union": "the European Union", "world": "the world"}
    return small.get(key, key.title())


def fetch_points(iso3: str, code: str, years: int = 15, http: Callable | None = None) -> tuple[list[tuple[int, float]], str]:
    """Most recent `years` non-empty yearly values, oldest first, and the date the World Bank last updated the series."""
    this = datetime.now().year
    res = _get(f"{API}/country/{iso3}/indicator/{code}",
               {"format": "json", "per_page": 200, "date": f"{this - years - 2}:{this}"}, http)
    if not isinstance(res, list) or len(res) < 2 or not res[1]:
        raise DataError(f"No data for {code} in {iso3}.")
    pts = sorted({(int(r["date"]), float(r["value"])) for r in res[1] if r.get("value") is not None and str(r.get("date", "")).isdigit()})
    if len(pts) < 3:
        raise DataError(f"Too little data for {code} in {iso3}.")
    return pts[-years:], str(res[0].get("lastupdated", ""))


def _n(v: float, places: int = 2) -> str:
    """7.0 -> "7", 3.870 -> "3.87" (a script that says "7 percent" must match the ledger's "7")."""
    return f"{v:.{places}f}".rstrip("0").rstrip(".")


def _human(v: float, kind: str) -> str:
    if kind == "pct":
        return f"{_n(v)} percent" if v >= 0 else f"minus {_n(abs(v))} percent"
    if kind == "usd":
        for div, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
            if abs(v) >= div:
                return f"US${_n(v / div)} {word}"
        return f"US${v:,.0f}"
    if kind == "people":
        for div, word in ((1e9, "billion"), (1e6, "million")):
            if abs(v) >= div:
                return f"{_n(v / div)} {word} people"
        return f"{v:,.0f} people"
    return _n(v, 1)


def _lc(label: str) -> str:
    return label[0].lower() + label[1:] if label[1:2].islower() else label     # keep "GDP", lower "Inflation"


def series_text(country: str, label: str, kind: str, pts: list[tuple[int, float]]) -> str:
    low = _lc(label)
    lines = [f"In {y}, {low} in {country} was {_human(v, kind)}." for y, v in pts]
    hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
    span = f"Between {pts[0][0]} and {pts[-1][0]}, {low} in {country}"
    lines.append(f"{span} was highest in {hi[0]} at {_human(hi[1], kind)}.")
    lines.append(f"{span} was lowest in {lo[0]} at {_human(lo[1], kind)}.")
    return "\n".join(lines)


def data_source(sid: str, country_name: str, iso3: str, key: str, pts: list[tuple[int, float]], updated: str) -> Source:
    code, label, kind = INDICATORS[key]
    url = f"https://data.worldbank.org/indicator/{code}?locations={iso3[:2]}"
    return Source(sid, url, f"World Bank: {label} - {country_name}", outlet_of(url), classify_tier(url), updated,
                  series_text(country_name, label, kind, pts),
                  {"label": label, "unit": kind, "country": country_name, "code": code,
                   "points": [[y, v] for y, v in pts]})


def gather_data(specs: list[dict[str, Any]], first_id: int = 1, years: int = 15,
                log: Callable[[str], None] = print, http: Callable | None = None) -> list[Source]:
    """specs: [{"country": "India", "indicators": ["gdp_growth", "inflation"]}, ...]"""
    out: list[Source] = []
    for spec in specs:
        try:
            iso3, name = resolve_country(spec.get("country", ""), http)
        except DataError as exc:
            log(f"[data] {exc}")
            continue
        for key in spec.get("indicators", []):
            if key not in INDICATORS:
                log(f"[data] unknown indicator '{key}'")
                continue
            try:
                pts, updated = fetch_points(iso3, INDICATORS[key][0], years, http)
            except Exception as exc:
                log(f"[data] {name} / {key}: {exc}")
                continue
            out.append(data_source(f"S{first_id + len(out)}", name, iso3, key, pts, updated))
            log(f"  data: {INDICATORS[key][1]} for {name} ({pts[0][0]}-{pts[-1][0]})")
    return out


def catalog() -> list[dict[str, str]]:
    return [{"key": k, "label": v[1]} for k, v in INDICATORS.items()]
