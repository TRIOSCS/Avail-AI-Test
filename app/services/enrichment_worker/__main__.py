"""Entry point: python -m app.services.enrichment_worker"""

import asyncio

from .worker import main

asyncio.run(main())
