from __future__ import annotations

import logging

from .missing_models_fetcher.nodes import MissingModelsFetcherStatus

NODE_CLASS_MAPPINGS = {
    "MissingModelsFetcherStatus": MissingModelsFetcherStatus,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MissingModelsFetcherStatus": "Missing Models Fetcher Status",
}
WEB_DIRECTORY = "./web"

try:
    from .missing_models_fetcher.routes import register_routes

    register_routes()
except Exception:
    logging.exception("[Missing Models Fetcher] Failed to register routes")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
