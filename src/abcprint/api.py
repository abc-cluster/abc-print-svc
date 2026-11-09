"""The ASGI app, for `uvicorn abcprint.api:app`.

The service is built by `abcprint.service.build()`; the image runs
`uvicorn --factory abcprint.service:build`. This module keeps the 0.14 import
path working, and builds the app from the environment at import — as 0.14 did.
"""
from .service import build

app = build()
