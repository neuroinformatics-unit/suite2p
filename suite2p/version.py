"""
Copyright © 2023 Howard Hughes Medical Institute, Authored by Carsen Stringer and Marius Pachitariu.
"""
from importlib_metadata import metadata as _metadata, PackageNotFoundError

try:
    version = _metadata("suite2p-NIU")["version"]
except PackageNotFoundError:
    version = _metadata("suite2p")["version"]
