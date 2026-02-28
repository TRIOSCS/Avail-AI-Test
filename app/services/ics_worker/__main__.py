"""Entry point for running ICS worker as a module.

Usage: python -m app.services.ics_worker.worker
"""

import asyncio

from .worker import main

asyncio.run(main())
