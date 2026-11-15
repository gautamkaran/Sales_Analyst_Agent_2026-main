"""Data-grounded analysis of sales records read from Google Sheets."""

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd

PERIOD_START = pd.Timestamp("2026-09-01")
PERIOD_END = pd.Timestamp("2026-10-01")
PERIOD_LABEL = "September 2026"

COLUMN_ALIASES = {
    "date": ("date", "sale date", "date of sale", "order date", "transaction date"),
    "product": ("product", "product name", "item", "item name", "sku name"),
    "region": ("region", "sales region", "territory", "sales territory"),
    "revenue": ("revenue", "total revenue", "net revenue", "sales revenue", "sales amount", "total sales", "amount"),
    "units": ("units sold", "unit sold", "units", "quantity sold", "quantity", "qty"),
    "unit_price": ("unit price", "price per unit", "selling price", "price"),
    "target": ("revenue target", "sales target", "regional target", "target revenue", "target sales", "target"),
}


def _normalise_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().casefold())


def _find_column(headers: List[str], aliases) -> Optional[str]:
    by_normalised = {_normalise_header(header): header for header in headers}
    for alias in aliases:
        key = _normalise_header(alias)
        if key in by_normalised:
            return by_normalised[key]
    return None


def _number(value: Any) -> float:
    """Parse common numeric/currency display strings without guessing values."""
    if value is None or isinstance(value, bool):
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return float("nan")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "")
    text = re.sub(r"[^0-9eE+.-]", "", text)
    if not text or text in {".", "+", "-"}:
        return float("nan")
    try:
        value_number = float(text)
        return -value_number if negative else value_number
    except ValueError:
        return float("nan")


def _parse_date(value: Any):
    if value is None or (isinstance(value, str) and not value.strip()):
        return pd.NaT
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.to_datetime(value, errors="coerce")
    # Google Sheets UNFORMATTED_VALUE represents date cells as serial days
    # since 1899-12-30. Limit this conversion to plausible spreadsheet dates.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 1 <= float(value) <= 100000:
            return pd.to_datetime(value, unit="D", origin="1899-12-30", errors="coerce")
    return pd.to_datetime(str(value).strip(), errors="coerce")


def _number_or_none(value) -> Optional[float]:
    if pd.isna(value):
        return None
    return float(value)


def _rank_dimension(frame: pd.DataFrame, column: Optional[str]) -> List[dict]:
    if not column or frame.empty:
        return []
    work = frame.loc[frame[column].notna() & frame[column].astype(str).str.strip().ne("")].copy()
    work = work.loc[work["_revenue"].notna()]
    if work.empty:
        return []
    work["_label"] = work[column].astype(str).str.strip()
    result = []
    for label, group in work.groupby("_label", sort=False):
        units = group["_units"].sum(min_count=1)
        result.append({
            "name": str(label),
            "revenue": float(group["_revenue"].sum()),
            "units_sold": _number_or_none(units),
            "revenue_rows": int(group["_revenue"].notna().sum()),
        })
    return sorted(result, key=lambda row: (-row["revenue"], row["name"].casefold()))


def _extreme(ranking: List[dict], highest: bool) -> Optional[dict]:
    if not ranking:
        return None
    value = (max if highest else min)(row["revenue"] for row in ranking)
    tied = [row["name"] for row in ranking if row["revenue"] == value]
    return {"names": tied, "revenue": float(value)}


def _recommendations(
    products: List[dict],
    regions: List[dict],
    target_comparisons: List[dict],
    rows_in_scope: int,
    revenue_rows: int,
    date_limitation: Optional[str],
) -> List[str]:
    recommendations = []
    if products:
        if len(products) > 1:
            low, high = products[-1], products[0]
            recommendations.append(
                "Review sales coverage for {0}: it recorded {1:,.2f} in revenue, "
                "compared with {2} at {3:,.2f}. Validate the underlying rows before "
                "changing product allocation.".format(
                    low["name"], low["revenue"], high["name"], high["revenue"]
                )
            )
        else:
            recommendations.append(
                "Track {0} in the next reporting period; it was the only product "
                "with usable revenue in this analysis.".format(products[0]["name"])
            )

    below_target = [row for row in target_comparisons if row["gap"] < 0]
    if below_target:
        lowest = min(below_target, key=lambda row: row["gap"])
        recommendations.append(
            "Prioritize a target review for {0}: recorded revenue was {1:,.2f} "
            "against a target of {2:,.2f}, a gap of {3:,.2f}.".format(
                lowest["region"], lowest["actual_revenue"], lowest["target"], -lowest["gap"]
            )
        )
    elif regions:
        if len(regions) > 1:
            low, high = regions[-1], regions[0]
            recommendations.append(
                "Compare sales coverage in {0} ({1:,.2f} revenue) with {2} "
                "({3:,.2f}); the observed regional difference is {4:,.2f}.".format(
                    low["name"], low["revenue"], high["name"], high["revenue"],
                    high["revenue"] - low["revenue"],
                )
            )
        else:
            recommendations.append(
                "Keep {0} as the regional baseline and compare it with other regions "
                "when additional regional records are available.".format(regions[0]["name"])
            )

    if rows_in_scope and revenue_rows < rows_in_scope:
        missing = rows_in_scope - revenue_rows
        recommendations.append(
            "Reconcile revenue for {0} of {1} in-scope rows before using the "
            "revenue ranking for decisions.".format(missing, rows_in_scope)
        )
    elif date_limitation:
        recommendations.append(
            "Add or standardize a usable date column so the next report can isolate "
            "September 2026 instead of combining the available periods."
        )
    elif products and regions:
        recommendations.append(
            "Use {0} and {1} as the leading product and region in the next performance "
            "review, and compare their revenue with the following reporting period.".format(
                products[0]["name"], regions[0]["name"]
            )
        )
    return recommendations[:3]


def analyse_sales_rows(source: Any) -> dict:
    """Calculate September 2026 sales metrics from a Sheets result or row list.

    If dates are missing or none can be parsed, all available records are used
    and the output explicitly records that the September filter was unavailable.
    """
    if isinstance(source, dict):
        raw_rows = source.get("rows", [])
        source_info = {
            "spreadsheet_id": source.get("spreadsheet_id"),
            "sheet_name": source.get("sheet_name"),
        }
        headers = list(source.get("headers") or [])
    elif isinstance(source, list):
        raw_rows = source
        source_info = {"spreadsheet_id": None, "sheet_name": None}
        headers = []
    else:
        raise TypeError("Sales data must be a Sheets result object or a list of row objects.")

    rows = [row for row in raw_rows if isinstance(row, dict)]
    if not headers and rows:
        headers = [key for key in rows[0].keys() if not str(key).startswith("_")]
    frame = pd.DataFrame(rows)
    for header in headers:
        if header not in frame.columns:
            frame[header] = pd.NA

    if frame.empty:
        frame = pd.DataFrame(columns=headers + ["_row_number"])

    date_column = _find_column(headers, COLUMN_ALIASES["date"])
    product_column = _find_column(headers, COLUMN_ALIASES["product"])
    region_column = _find_column(headers, COLUMN_ALIASES["region"])
    revenue_column = _find_column(headers, COLUMN_ALIASES["revenue"])
    units_column = _find_column(headers, COLUMN_ALIASES["units"])
    unit_price_column = _find_column(headers, COLUMN_ALIASES["unit_price"])
    target_column = _find_column(headers, COLUMN_ALIASES["target"])

    data_quality = []
    period_limited = False
    date_valid_count = 0
    excluded_date_rows = 0
    period_scope = frame.copy()
    if date_column:
        parsed_dates = frame[date_column].map(_parse_date)
        parsed_dates = parsed_dates.map(lambda value: value.tz_localize(None) if isinstance(value, pd.Timestamp) and value.tz is not None else value)
        date_valid_count = int(parsed_dates.notna().sum())
        if date_valid_count:
            period_mask = parsed_dates.ge(PERIOD_START) & parsed_dates.lt(PERIOD_END)
            excluded_date_rows = int((~period_mask).sum())
            period_scope = frame.loc[period_mask].copy()
            if period_scope.empty:
                data_quality.append("No dated rows fell within September 2026; no rows were included in this period's totals.")
            period_scope["_parsed_date"] = parsed_dates.loc[period_mask]
            if int(parsed_dates.isna().sum()):
                data_quality.append(
                    "{} row(s) with missing or unparseable dates were excluded from "
                    "the September 2026 period filter.".format(int(parsed_dates.isna().sum()))
                )
        else:
            period_limited = True
            data_quality.append(
                "A date column ({}) exists, but none of its values could be parsed; "
                "all available records were analyzed without a September filter.".format(date_column)
            )
    else:
        period_limited = True
        data_quality.append(
            "No recognizable date column was found; all available records were analyzed "
            "without a September 2026 filter."
        )

    if revenue_column:
        period_scope["_revenue"] = period_scope[revenue_column].map(_number)
        revenue_source = "column: {}".format(revenue_column)
    elif units_column and unit_price_column:
        period_scope["_units_for_revenue"] = period_scope[units_column].map(_number)
        period_scope["_price_for_revenue"] = period_scope[unit_price_column].map(_number)
        period_scope["_revenue"] = period_scope["_units_for_revenue"] * period_scope["_price_for_revenue"]
        revenue_source = "calculated as {} multiplied by {} because no revenue column was available".format(
            units_column, unit_price_column
        )
        data_quality.append("Revenue was calculated as units multiplied by unit price for each row.")
    else:
        period_scope["_revenue"] = float("nan")
        revenue_source = None
        data_quality.append(
            "No revenue column or supported units-and-unit-price pair was found; "
            "revenue totals and revenue rankings are unavailable."
        )

    if units_column:
        period_scope["_units"] = period_scope[units_column].map(_number)
    elif "_units_for_revenue" in period_scope:
        period_scope["_units"] = period_scope["_units_for_revenue"]
    else:
        period_scope["_units"] = float("nan")
        data_quality.append("No recognizable units-sold or quantity column was found.")

    revenue_valid_count = int(period_scope["_revenue"].notna().sum())
    units_valid_count = int(period_scope["_units"].notna().sum())
    if revenue_valid_count < len(period_scope) and revenue_source:
        data_quality.append(
            "Revenue was usable for {} of {} in-scope rows.".format(revenue_valid_count, len(period_scope))
        )
    if units_column and units_valid_count < len(period_scope):
        data_quality.append(
            "Units were usable for {} of {} in-scope rows.".format(units_valid_count, len(period_scope))
        )

    revenue_total = (
        float(period_scope["_revenue"].sum()) if revenue_valid_count else None
    )
    units_total = float(period_scope["_units"].sum()) if units_valid_count else None
    products = _rank_dimension(period_scope, product_column)
    regions = _rank_dimension(period_scope, region_column)

    target_comparisons = []
    if target_column and region_column and revenue_valid_count:
        by_region = {row["name"]: row for row in regions}
        for region_name, group in period_scope.groupby(region_column, dropna=True, sort=False):
            region_name = str(region_name).strip()
            if not region_name or region_name not in by_region:
                continue
            targets = [value for value in group[target_column].map(_number).dropna().unique()]
            if len(targets) == 1:
                target = float(targets[0])
                actual = by_region[region_name]["revenue"]
                gap = actual - target
                target_comparisons.append({
                    "region": region_name,
                    "actual_revenue": float(actual),
                    "target": target,
                    "gap": float(gap),
                    "attainment_percent": (float(actual / target * 100) if target != 0 else None),
                })
            elif len(targets) > 1:
                data_quality.append(
                    "Regional target values for {} vary within the selected rows; "
                    "that region's target comparison was omitted.".format(region_name)
                )
    elif target_column and not region_column:
        data_quality.append("Target data was present, but no region column was available for comparison.")

    if target_column and not target_comparisons and not any("target values" in note for note in data_quality):
        data_quality.append(
            "A target column was found, but no unambiguous regional target comparison could be calculated."
        )

    recommendations = _recommendations(
        products, regions, target_comparisons, len(period_scope), revenue_valid_count,
        "date filter unavailable" if period_limited else None,
    )
    if not period_scope.empty and not recommendations:
        data_quality.append(
            "The available fields do not support three reliable business recommendations."
        )

    return {
        **source_info,
        "period": PERIOD_LABEL if not period_limited else "All available records",
        "period_filter_applied": not period_limited and bool(date_column and date_valid_count),
        "period_limitation": (
            "September 2026 could not be isolated; all available records were analyzed."
            if period_limited else None
        ),
        "date_column": date_column,
        "rows_available": int(len(rows)),
        "rows_in_scope": int(len(period_scope)),
        "rows_with_valid_date": date_valid_count,
        "rows_excluded_by_period_or_date": excluded_date_rows,
        "revenue_column_or_formula": revenue_source,
        "total_revenue": revenue_total,
        "revenue_rows": revenue_valid_count,
        "units_sold": units_total,
        "units_rows": units_valid_count,
        "revenue_by_product": products,
        "revenue_by_region": regions,
        "top_products": _extreme(products, highest=True),
        "lowest_products": _extreme(products, highest=False),
        "top_regions": _extreme(regions, highest=True),
        "lowest_regions": _extreme(regions, highest=False),
        "regional_target_comparisons": target_comparisons,
        "recommendations": recommendations,
        "data_quality_notes": data_quality,
    }


def analyse_sales() -> dict:
    """Read the configured worksheet and return calculated September results."""
    from google_sheets import read_sales_rows
    return analyse_sales_rows(read_sales_rows())
