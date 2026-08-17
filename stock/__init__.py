"""Public package for the Vietnamese stock-analysis subsystem.

Application code should import stock functionality through ``stock.*``.
Modules remain lazy so importing one provider does not initialize the entire
analysis and backtesting stack.
"""

__all__ = [
    "analysis",
    "backtest",
    "features",
    "fundamentals",
    "policy",
    "providers",
    "sector",
    "validation",
]
