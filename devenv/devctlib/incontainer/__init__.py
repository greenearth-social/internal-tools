"""Programs devctl runs inside a service's container, as `python -c <source>`.

They import the service's own code (the api's FastAPI app, BridgEU's
publishing helpers), which exists only in its container, so they're never
imported here — read them with `source(name)`.
"""

from pathlib import Path


def source(name: str) -> str:
    return (Path(__file__).parent / f"{name}.py").read_text()
