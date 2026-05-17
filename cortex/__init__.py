"""hermes-cortex — cognitive architecture and memory lifecycle for Hermes-based assistants."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("hermes-cortex")
except PackageNotFoundError:
    # Fallback for dev environments where the package isn't installed yet.
    __version__ = "0.0.0-dev"
