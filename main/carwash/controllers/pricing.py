"""Simulated pricing: base price covers the included time, extra time is billed per started minute."""


def price_for_seconds(settings: dict, seconds: int) -> int:
    """Price in integer cents for `seconds` of run time, using a bay's pricing settings."""
    extra_seconds = max(0, seconds - settings["included_seconds"])
    extra_minutes = (extra_seconds + 59) // 60
    return settings["base_price_cents"] + extra_minutes * settings["additional_price_per_minute_cents"]


def price_after_adding(settings: dict, active: dict, seconds: int) -> int:
    """New total for a wash in progress after adding `seconds`. Only the added time is
    charged at the bay's current prices, so changing the price mid-wash doesn't reprice
    the time the customer already paid for."""
    granted = active["granted_seconds"]
    added = price_for_seconds(settings, granted + seconds) - price_for_seconds(settings, granted)
    return active["price_cents"] + max(0, added)
