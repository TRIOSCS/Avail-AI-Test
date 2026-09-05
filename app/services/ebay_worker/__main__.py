"""Entry point for running the eBay worker as a module.

Usage: python -m app.services.ebay_worker.worker
"""

import asyncio  # pragma: no cover

from .worker import main  # pragma: no cover

asyncio.run(main())  # pragma: no cover
