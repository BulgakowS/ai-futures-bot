"""One public MEXC catalog GET; outputs research candidates only, no prices or orders."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from ai_futures.mexc_zero_fee_catalog import (
    CatalogError,
    fetch_public_zero_fee_catalog,
    sanitized_catalog,
)


async def run(output: Path | None) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        catalog = await fetch_public_zero_fee_catalog(client)
    sanitized = sanitized_catalog(catalog)
    rendered = json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(rendered, end="")
    else:
        output.write_text(rendered, encoding="utf-8")
        print(
            f"Public research catalog: {sanitized['candidate_count']} candidates, "
            f"{sanitized['unknown_rows']} unknown, {sanitized['invalid_rows']} invalid; "
            f"saved to {output}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write only sanitized candidate JSON")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.output))
    except CatalogError as exc:
        parser.exit(1, f"Public catalog unavailable: {exc}\n")
