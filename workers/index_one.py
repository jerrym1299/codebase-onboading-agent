import argparse
import asyncio
import json
import time

from services.db import close_pool, create_repo_index_job, init_schema
from workers.job_runner import process_repo_index_job


async def _main(args: argparse.Namespace) -> dict:
    await init_schema()
    try:
        job_id = args.job_id
        if job_id is None:
            job = await create_repo_index_job(
                repo_url=args.repo_url.rstrip("/"),
                requested_by=args.requested_by,
                trigger=args.trigger,
                target_ref=args.target_ref,
                priority=args.priority,
                metadata={"source": "index_one"},
            )
            job_id = job["id"]

        result = await process_repo_index_job(
            job_id,
            generate_summaries=not args.skip_summaries,
        )
        return {"job_id": job_id, "result": result}
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or process one repo_index_jobs row.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job-id")
    target.add_argument("--repo-url")
    parser.add_argument("--requested-by", default="index_one")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--target-ref", default="HEAD")
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--skip-summaries", action="store_true")
    parser.add_argument("--idle-after-complete-seconds", type=int, default=0)
    args = parser.parse_args()
    result = asyncio.run(_main(args))
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    if args.idle_after_complete_seconds > 0:
        print(
            f"Index smoke complete; idling for {args.idle_after_complete_seconds}s.",
            flush=True,
        )
        time.sleep(args.idle_after_complete_seconds)


if __name__ == "__main__":
    main()
