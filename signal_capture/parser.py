from dataclasses import dataclass
from decimal import Decimal
import re


@dataclass(frozen=True)
class Signal:
    side: str
    entry: float
    stop: float
    target: float


def parse_signal(text: str) -> Signal:
    # Full-match prevents an update, cancellation, second trade or extra TP
    # from being interpreted as an instruction to open a new position.
    cleaned = text.replace("\ufe0f", "").replace("**", "").strip()
    number = r"([0-9]+(?:\.[0-9]+)?)"
    pattern = rf"(LONG|SHORT)\s+entry\s+{number}\s*\n\s*🎯\s*{number}\s*\n\s*🛑\s*{number}"
    match = re.fullmatch(pattern, cleaned, re.IGNORECASE)
    if not match:
        raise ValueError("Unsupported signal format; expected entry, one target and one stop")
    side, entry, target, stop = match.groups()
    values = [Decimal(x) for x in (entry, stop, target)]
    if any(x <= 0 or not x.is_finite() for x in values):
        raise ValueError("Prices must be positive and finite")
    entry, stop, target = map(float, values)
    import math
    if not all(math.isfinite(x) for x in (entry, stop, target)):
        raise ValueError("Price outside supported range")
    if not (stop < entry < target if side.upper() == "LONG" else target < entry < stop):
        raise ValueError("Stop and target are on the wrong sides of entry")
    return Signal(side.upper(), entry, stop, target)


def extract_text(message):
    parts = [message.content] if message.content.strip() else []
    for embed in message.embeds:
        parts.extend(x for x in (embed.title, embed.description) if x)
        for field in embed.fields:
            parts.append(f"{field.name} {field.value}")
    return "\n".join(parts)
