import argparse
import asyncio
import json

from services.db import close_pool, init_schema
from workers.job_runner import process_repo_index_job


async def _main(job_id: str) -> dict:
    await init_schema()
    try:
        return await process_repo_index_job(job_id)
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process one repo_index_jobs row.")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    result = asyncio.run(_main(args.job_id))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
