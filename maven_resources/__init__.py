"""Non-code resources shipped in the installed MAVEN distribution."""
from pathlib import Path


def resource_path(relative: str) -> Path:
    """Return an existing resource inside this unpacked installed package."""
    requested = Path(relative)
    if not relative or requested.is_absolute() or '..' in requested.parts:
        raise ValueError('invalid_resource_path')
    base = Path(__file__).resolve().parent
    result = (base / requested).resolve(strict=True)
    if not result.is_relative_to(base):
        raise ValueError('invalid_resource_path')
    return result
