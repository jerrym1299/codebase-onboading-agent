import argparse
import asyncio
import json
import logging

from services.db import claim_next_repo_index_job, close_pool, init_schema
from workers.job_runner import process_repo_index_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _run_loop(*, once: bool, poll_seconds: float) -> None:
    await init_schema()
    try:
        while True:
            job = await claim_next_repo_index_job()
            if job is None:
                if once:
                    return
                await asyncio.sleep(poll_seconds)
                continue

            logger.info("Processing repo_index_job %s", job["id"])
            try:
                result = await process_repo_index_job(job["id"])
                logger.info(
                    "Completed repo_index_job %s: %s",
                    job["id"],
                    json.dumps({
                        "status": result["status"],
                        "repo_index_id": result["repo_index_id"],
                    }),
                )
            except Exception:
                logger.exception("Failed repo_index_job %s", job["id"])
                if once:
                    raise

            if once:
                return
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll and process repo_index_jobs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    asyncio.run(_run_loop(once=args.once, poll_seconds=args.poll_seconds))


if __name__ == "__main__":
    main()
