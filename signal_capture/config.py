from dataclasses import dataclass
import math
import os
from pathlib import Path
import tomllib

SOURCE_BOT_ID = 1547302271942000751


def positive(value, name, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True)
class Channel:
    symbol: str
    max_entry_drift_points: float
    max_spread_points: float
    deviation_points: int
    commission_per_lot: float = 0


@dataclass(frozen=True)
class Config:
    channels: dict[int, Channel]
    risk_percent: float = 0.5
    dry_run: bool = True
    allow_real_account: bool = False
    max_signal_age_seconds: int = 60
    max_tick_age_seconds: int = 15
    max_positions: int = 1
    magic: int = 1547302


def load_config(path=None):
    with Path(path or os.getenv("CONFIG_PATH", "config.toml")).open("rb") as f:
        raw = tomllib.load(f)
    channels = {}
    for key, value in raw.pop("channels", {}).items():
        channel_id = int(key)
        if channel_id <= 0:
            raise ValueError("Channel IDs must be positive")
        ch = Channel(**value)
        if not ch.symbol.strip() or "REPLACE" in ch.symbol:
            raise ValueError("An exact broker symbol is required")
        for field in ("max_entry_drift_points", "max_spread_points", "deviation_points", "commission_per_lot"):
            positive(getattr(ch, field), field, allow_zero=True)
        if type(ch.deviation_points) is not int:
            raise ValueError("deviation_points must be an integer")
        channels[channel_id] = ch
    if not channels:
        raise ValueError("Configure at least one Discord channel")
    cfg = Config(channels=channels, **raw)
    if type(cfg.dry_run) is not bool or type(cfg.allow_real_account) is not bool:
        raise ValueError("Execution flags must be TOML booleans")
    for field in ("risk_percent", "max_signal_age_seconds", "max_tick_age_seconds", "max_positions", "magic"):
        positive(getattr(cfg, field), field)
    if cfg.risk_percent > 100:
        raise ValueError("risk_percent must be at most 100")
    for field in ("max_positions", "magic", "max_signal_age_seconds", "max_tick_age_seconds"):
        if type(getattr(cfg, field)) is not int:
            raise ValueError(f"{field} must be an integer")
    return cfg
