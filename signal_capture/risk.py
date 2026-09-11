from decimal import Decimal, ROUND_FLOOR
import math


def size_volume(budget, loss_per_lot, minimum, maximum, step):
    values = (budget, loss_per_lot, minimum, maximum, step)
    if any(not math.isfinite(x) or x <= 0 for x in values) or maximum < minimum:
        raise ValueError("Invalid broker volume or risk values")
    budget, loss, minimum, maximum, step = map(lambda x: Decimal(str(x)), values)
    raw = min(budget / loss, maximum)
    volume = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if volume < minimum:
        raise ValueError("Minimum broker lot would exceed the risk budget")
    return float(volume)
