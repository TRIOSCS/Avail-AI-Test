"""Entry point for running ICS worker as a module.

Usage: python -m app.services.ics_worker.worker
"""

import asyncio  # pragma: no cover

from .worker import main  # pragma: no cover

asyncio.run(main())  # pragma: no cover
