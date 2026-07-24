"""TrueFoundry annotation-driven NetworkPolicy operator."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version in pyproject.toml, read from the
    # installed package metadata so release bumps can't drift out of sync.
    __version__ = version("tfy-netpol-operator")
except PackageNotFoundError:
    # Running from a source checkout without `pip install (-e) .`.
    __version__ = "0.0.0+unknown"
