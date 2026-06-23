"""Human behavior simulation for browser automation.

Static async methods that simulate realistic human interaction patterns:
typing speed variation, random click positions, gaussian-distributed delays.

Called by: session_manager (login flow), search_engine
Depends on: asyncio, random
"""

import asyncio
import random


class HumanBehavior:
    """Simulates human-like browser interaction patterns."""

    @staticmethod
    async def random_delay(min_sec: float, max_sec: float):
        """Sleep for a random duration using gaussian distribution biased toward the
        middle."""
        mean = (min_sec + max_sec) / 2
        std_dev = (max_sec - min_sec) / 4
        delay = random.gauss(mean, std_dev)
        delay = max(min_sec, min(max_sec, delay))
        await asyncio.sleep(delay)

    @staticmethod
    async def human_type(page, locator, text: str):
        """Type text character-by-character with human-like speed variation.

        Simulates variable typing speed (80-200ms per char) with occasional "thinking
        pauses" (5% chance per character, 0.4-1.2s).
        """
        await locator.click()
        await asyncio.sleep(random.uniform(0.1, 0.3))

        for char in text:
            await page.keyboard.type(char)
            delay_ms = random.uniform(80, 200) / 1000
            if random.random() < 0.05:
                delay_ms += random.uniform(0.4, 1.2)
            await asyncio.sleep(delay_ms)

    @staticmethod
    async def human_click(page, locator):
        """Click an element at a slightly randomized position within its bounding
        box."""
        box = await locator.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.click(x, y)
        else:
            await locator.click()
