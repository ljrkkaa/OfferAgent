"""Pillow adapter restricted to the explicitly declared attachment format."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from typing import Protocol, cast

from ..errors import BackendExecutionError, BackendInputError, BackendOutputError, BackendUnavailableError
from ..interfaces import BackendIdentity, ImageInfo, RasterImage

_PILLOW_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}


class _PillowImage(Protocol):
    @property
    def format(self) -> str | None: ...

    @property
    def size(self) -> tuple[int, int]: ...

    @property
    def n_frames(self) -> int: ...

    def verify(self) -> None: ...

    def seek(self, frame: int) -> None: ...

    def convert(self, mode: str) -> _PillowImage: ...

    def load(self) -> object: ...

    def tobytes(self) -> bytes: ...

    def close(self) -> None: ...


class _PillowModule(Protocol):
    def open(self, fp: BytesIO, mode: str = "r", formats: list[str] | None = None) -> _PillowImage: ...


class PillowImageDecoder:
    def __init__(self, module: _PillowModule, *, package_version: str) -> None:
        if not package_version:
            raise ValueError("Pillow package version must not be empty")
        self._module = module
        self._identity = BackendIdentity(name="pillow", version=package_version)

    @classmethod
    def from_installed(cls) -> PillowImageDecoder:
        try:
            module = cast(_PillowModule, import_module("PIL.Image"))
            package_version = version("Pillow")
        except (ImportError, PackageNotFoundError) as error:
            raise BackendUnavailableError("pillow") from error
        return cls(module, package_version=package_version)

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    def inspect(self, data: bytes, *, media_type: str) -> ImageInfo:
        expected_format = self._expected_format(media_type)
        self._validate_webp_container(data, media_type=media_type)
        image = self._open(data, expected_format=expected_format)
        try:
            self._require_format(image, expected_format=expected_format)
            width, height = image.size
            frame_count = 1 if expected_format == "JPEG" else image.n_frames
            image.verify()
        except BackendInputError:
            raise
        except Exception as error:
            raise BackendInputError(self._identity.name, "malformed_image") from error
        finally:
            image.close()
        try:
            return ImageInfo(width=width, height=height, frame_count=frame_count)
        except (TypeError, ValueError) as error:
            raise BackendOutputError(self._identity.name, "image metadata is invalid") from error

    def decode_frame_rgb(self, data: bytes, *, media_type: str, frame_index: int) -> RasterImage:
        expected_format = self._expected_format(media_type)
        image = self._open(data, expected_format=expected_format)
        converted: _PillowImage | None = None
        try:
            self._require_format(image, expected_format=expected_format)
            image.seek(frame_index)
            converted = image.convert("RGB")
            converted.load()
            width, height = converted.size
            pixels = converted.tobytes()
        except BackendInputError:
            raise
        except (EOFError, IndexError) as error:
            raise BackendInputError(self._identity.name, "missing_image_frame") from error
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "decode_frame_rgb") from error
        finally:
            if converted is not None:
                converted.close()
            image.close()
        try:
            return RasterImage(width=width, height=height, rgb=pixels)
        except (TypeError, ValueError) as error:
            raise BackendOutputError(self._identity.name, "decoded frame is not packed RGB") from error

    def _open(self, data: bytes, *, expected_format: str) -> _PillowImage:
        try:
            return self._module.open(BytesIO(data), mode="r", formats=[expected_format])
        except Exception as error:
            raise BackendInputError(self._identity.name, "malformed_image") from error

    def _require_format(self, image: _PillowImage, *, expected_format: str) -> None:
        if image.format != expected_format:
            raise BackendInputError(self._identity.name, "decoded_format_mismatch")

    def _expected_format(self, media_type: str) -> str:
        try:
            return _PILLOW_FORMATS[media_type]
        except KeyError as error:
            raise BackendInputError(self._identity.name, "unsupported_declared_media_type") from error

    def _validate_webp_container(self, data: bytes, *, media_type: str) -> None:
        if media_type != "image/webp":
            return
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise BackendInputError(self._identity.name, "invalid_webp_signature")
        declared_size = int.from_bytes(data[4:8], byteorder="little", signed=False) + 8
        if declared_size != len(data):
            raise BackendInputError(self._identity.name, "invalid_webp_container_size")


__all__ = ["PillowImageDecoder"]
