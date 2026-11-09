"""Simulated pricing: base price covers the included time, extra time is billed per started minute."""


def price_for_seconds(settings: dict, seconds: int) -> int:
    """Price in integer cents for `seconds` of run time, using a bay's pricing settings."""
    extra_seconds = max(0, seconds - settings["included_seconds"])
    extra_minutes = (extra_seconds + 59) // 60
    return settings["base_price_cents"] + extra_minutes * settings["additional_price_per_minute_cents"]
