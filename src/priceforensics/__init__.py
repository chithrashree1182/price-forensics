"""Price Forensics — measuring discount integrity in Indian e-commerce.

A longitudinal study of advertised versus actual discounts. Collects daily
prices, detects pre-sale price inflation, and audits whether retailers agree on
the MRP a discount is calculated from.

See docs/methodology.md for the analytical approach and docs/ethics.md for the
collection policy.
"""

__version__ = "0.4.0"

__all__ = ["collect", "db", "export", "normalize", "synthetic"]
