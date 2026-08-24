"""PyMuPDF adapter implementing the dependency-neutral PDF interface."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol, cast

from ..errors import (
    BackendExecutionError,
    BackendInputError,
    BackendOutputError,
    BackendUnavailableError,
    EncryptedPdfError,
)
from ..interfaces import BackendIdentity, PdfDocument, RasterImage


class _Rect(Protocol):
    @property
    def width(self) -> float: ...

    @property
    def height(self) -> float: ...


class _Pixmap(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...

    @property
    def samples(self) -> bytes: ...


class _Page(Protocol):
    @property
    def rect(self) -> _Rect: ...

    def get_text(self, option: str, *, sort: bool) -> str: ...

    def get_pixmap(self, *, dpi: int, colorspace: object, alpha: bool) -> _Pixmap: ...


class _Document(Protocol):
    @property
    def needs_pass(self) -> bool: ...

    @property
    def page_count(self) -> int: ...

    def load_page(self, page_id: int) -> _Page: ...

    def close(self) -> None: ...


class _PyMuPdfModule(Protocol):
    @property
    def csRGB(self) -> object: ...

    def open(self, *, stream: bytes, filetype: str) -> _Document: ...


class _PyMuPdfDocument(PdfDocument):
    def __init__(self, document: _Document, module: _PyMuPdfModule, identity: BackendIdentity) -> None:
        self._document = document
        self._module = module
        self._identity = identity

    @property
    def page_count(self) -> int:
        try:
            return self._document.page_count
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "page_count") from error

    def extract_text(self, page_index: int, *, sort: bool) -> str:
        try:
            value = self._document.load_page(page_index).get_text("text", sort=sort)
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "extract_text") from error
        if not isinstance(value, str):
            raise BackendOutputError(self._identity.name, "get_text returned a non-string value")
        return value

    def page_size_points(self, page_index: int) -> tuple[float, float]:
        try:
            rect = self._document.load_page(page_index).rect
            return float(rect.width), float(rect.height)
        except (TypeError, ValueError) as error:
            raise BackendOutputError(self._identity.name, "page rectangle is not numeric") from error
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "page_size") from error

    def render_rgb(self, page_index: int, *, dpi: int) -> RasterImage:
        try:
            pixmap = self._document.load_page(page_index).get_pixmap(
                dpi=dpi,
                colorspace=self._module.csRGB,
                alpha=False,
            )
            width = pixmap.width
            height = pixmap.height
            samples = bytes(pixmap.samples)
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "render_rgb") from error
        try:
            return RasterImage(width=width, height=height, rgb=samples)
        except (TypeError, ValueError) as error:
            raise BackendOutputError(self._identity.name, "rendered pixmap is not packed RGB") from error

    def close(self) -> None:
        try:
            self._document.close()
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "close") from error


class PyMuPdfBackend:
    """Loads only the modern ``pymupdf`` package; no legacy import fallback."""

    def __init__(self, module: _PyMuPdfModule, *, package_version: str) -> None:
        if not package_version:
            raise ValueError("PyMuPDF package version must not be empty")
        self._module = module
        self._identity = BackendIdentity(name="pymupdf", version=package_version)

    @classmethod
    def from_installed(cls) -> PyMuPdfBackend:
        try:
            module = cast(_PyMuPdfModule, import_module("pymupdf"))
            package_version = version("PyMuPDF")
        except (ImportError, PackageNotFoundError) as error:
            raise BackendUnavailableError("pymupdf") from error
        return cls(module, package_version=package_version)

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    def open_pdf(self, data: bytes) -> PdfDocument:
        try:
            document = self._module.open(stream=data, filetype="pdf")
        except Exception as error:
            raise BackendInputError(self._identity.name, "malformed_pdf") from error
        try:
            if document.needs_pass:
                document.close()
                raise EncryptedPdfError(self._identity.name)
        except EncryptedPdfError:
            raise
        except Exception as error:
            try:
                document.close()
            except Exception:
                pass
            raise BackendExecutionError(self._identity.name, "inspect_encryption") from error
        return _PyMuPdfDocument(document, self._module, self._identity)


__all__ = ["PyMuPdfBackend"]
