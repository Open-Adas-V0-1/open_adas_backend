"""DIAGNOSTIC ONLY (throwaway) -- lists every (thread_id, checkpoint_ns) currently
stored in the production checkpointer, along with supervisor_visits/done/plan_state
for each, to find out whether the graph's real running thread_id matches
str(session_id) and whether this script is even looking at the same database the
dev server uses.

Usage: python scripts/_debug_threads.py
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import get_settings
from harness.checkpointer import build_production_checkpointer


async def main():
    settings = get_settings()
    print(f"db_host={settings.db_host} db_port={settings.db_port} db_name={settings.db_name} "
          f"db_user={settings.db_user}")
    print(f"checkpointer_database_url={settings.checkpointer_database_url}")
    print(f"checkpoint_encryption_key_set={bool(settings.checkpoint_encryption_key)}")
    print("-" * 80)

    async with build_production_checkpointer() as checkpointer:
        count = 0
        async for tup in checkpointer.alist(None):
            count += 1
            config = tup.config or {}
            configurable = config.get("configurable", {})
            thread_id = configurable.get("thread_id")
            checkpoint_ns = configurable.get("checkpoint_ns")
            checkpoint_id = configurable.get("checkpoint_id")
            values = (tup.checkpoint or {}).get("channel_values", {})
            print(
                f"thread_id={thread_id!r} checkpoint_ns={checkpoint_ns!r} "
                f"checkpoint_id={checkpoint_id!r} "
                f"supervisor_visits={values.get('supervisor_visits')!r} "
                f"done={values.get('done')!r} "
                f"plan_state={'<present>' if values.get('plan_state') else values.get('plan_state')!r} "
                f"classification={values.get('classification')!r} "
                f"result={values.get('result')!r}"
            )
        print("-" * 80)
        print(f"total checkpoint rows returned by alist(None): {count}")


if __name__ == "__main__":
    asyncio.run(main())
