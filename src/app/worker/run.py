import logging
import signal
import sys
import threading

import redis
from rq import Queue, Worker

from app.config import get_settings
from app.worker.cleanup import sweep_once

log = logging.getLogger("ydtl.run")


def run_worker() -> None:
    """RQ worker with the scheduler enabled (needed for the delayed deletes)."""
    # No socket_timeout: a worker blocks on Redis for long stretches.
    r = redis.Redis.from_url(get_settings().redis_url)
    worker = Worker([Queue("default", connection=r)], connection=r)
    worker.work(with_scheduler=True)  # handles SIGINT/SIGTERM itself


def run_sweeper() -> None:
    """Run the sweeper every SWEEP_INTERVAL_SECONDS until stopped."""
    interval = get_settings().sweep_interval_seconds
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("Sweeper started, interval %ss", interval)
    while not stop.is_set():
        try:
            sweep_once()
        except Exception:
            # Redis or storage may be briefly down; keep looping.
            log.exception("Sweep pass failed")
        stop.wait(interval)  # sleeps, but wakes immediately on shutdown
    log.info("Sweeper stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    modes = {"worker": run_worker, "sweeper": run_sweeper}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        sys.exit("usage: python -m app.worker.run [worker|sweeper]")
    modes[sys.argv[1]]()


if __name__ == "__main__":
    main()
