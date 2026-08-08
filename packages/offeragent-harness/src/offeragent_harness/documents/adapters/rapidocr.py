"""RapidOCR adapter with explicit model and raster color-order identity."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
from collections.abc import Callable, Mapping, Sequence
from ctypes import CDLL
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast

from offeragent_harness.foundation.canonical import canonical_json_sha256

from ..errors import BackendExecutionError, BackendInputError, BackendOutputError, BackendUnavailableError
from ..interfaces import BackendIdentity, OcrPoint, OcrRegion, RasterImage


class RasterArrayColorOrder(str, Enum):
    RGB = "rgb"
    BGR = "bgr"


class RapidOcrExecutionProvider(str, Enum):
    CPU = "cpu"
    CUDA = "cuda"


@dataclass(frozen=True, slots=True)
class RapidOcrExecutionProfile:
    provider: RapidOcrExecutionProvider
    device_id: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.device_id, bool) or not isinstance(self.device_id, int) or self.device_id < 0:
            raise ValueError("RapidOCR device_id must be a non-negative integer")

    def to_json(self) -> dict[str, object]:
        return {"provider": self.provider.value, "deviceId": self.device_id}


class _RapidOutput(Protocol):
    @property
    def boxes(self) -> Sequence[Sequence[Sequence[float]]] | None: ...

    @property
    def txts(self) -> Sequence[str] | None: ...

    @property
    def scores(self) -> Sequence[float] | None: ...


class _RapidEngine(Protocol):
    @property
    def text_det(self) -> _RapidComponent: ...

    @property
    def text_cls(self) -> _RapidComponent: ...

    @property
    def text_rec(self) -> _RapidComponent: ...

    def __call__(self, image: object) -> _RapidOutput: ...


class _ProviderSession(Protocol):
    def get_providers(self) -> Sequence[str]: ...


class _RapidInferenceSession(Protocol):
    @property
    def session(self) -> _ProviderSession: ...


class _RapidComponent(Protocol):
    @property
    def session(self) -> _RapidInferenceSession: ...


class _OnnxRuntimeModule(Protocol):
    def preload_dlls(
        self,
        *,
        cuda: bool = True,
        cudnn: bool = True,
        msvc: bool = True,
        directory: str | None = None,
    ) -> None: ...

    def get_available_providers(self) -> Sequence[str]: ...

    def get_device(self) -> str: ...


class _RapidModule(Protocol):
    @property
    def EngineType(self) -> _EngineTypeValues: ...

    def RapidOCR(self, *, params: Mapping[str, object]) -> _RapidEngine: ...


class _EngineTypeValues(Protocol):
    @property
    def ONNXRUNTIME(self) -> object: ...


RasterArrayConverter = Callable[[RasterImage], object]
_CUDA_DLL_DIRECTORY_HANDLES: list[object] = []
_CUDA_DLL_HANDLES: list[CDLL] = []
_CUDA_PRELOAD_LOCK = Lock()


def _load_runtime_dependency(name: str) -> Any:
    """Keep optional native dependency stubs outside the dependency-neutral type graph."""

    return import_module(name)


@dataclass(frozen=True, slots=True)
class LocalModelFile:
    relative_path: str
    byte_size: int
    sha256: str

    def __post_init__(self) -> None:
        path = Path(self.relative_path)
        if not self.relative_path or path.is_absolute() or ".." in path.parts:
            raise ValueError("RapidOCR model paths must be relative paths contained by model_root")
        if self.byte_size <= 0:
            raise ValueError("RapidOCR model byte_size must be positive")
        if len(self.sha256) != 71 or not self.sha256.startswith("sha256:"):
            raise ValueError("RapidOCR model sha256 must be a lowercase SHA-256 identity")
        try:
            int(self.sha256[7:], 16)
        except ValueError as error:
            raise ValueError("RapidOCR model sha256 must be a lowercase SHA-256 identity") from error
        if self.sha256.lower() != self.sha256:
            raise ValueError("RapidOCR model sha256 must be lowercase")


@dataclass(frozen=True, slots=True)
class RapidOcrLocalModels:
    model_root: Path
    detector: LocalModelFile
    classifier: LocalModelFile
    recognizer: LocalModelFile
    recognition_keys: LocalModelFile | None

    def __post_init__(self) -> None:
        if not self.model_root.is_absolute():
            raise ValueError("RapidOCR model_root must be absolute")

    def verify(self) -> tuple[dict[str, str], str]:
        try:
            root = self.model_root.resolve(strict=True)
        except OSError as error:
            raise BackendInputError("rapidocr", "model_root_not_found") from error
        if self.model_root.is_symlink() or not root.is_dir():
            raise BackendInputError("rapidocr", "model_root_must_be_a_non_symlink_directory")

        resolved = {
            "detector": self._verify_file(root, self.detector),
            "classifier": self._verify_file(root, self.classifier),
            "recognizer": self._verify_file(root, self.recognizer),
        }
        if self.recognition_keys is not None:
            resolved["recognitionKeys"] = self._verify_file(root, self.recognition_keys)
        manifest = {
            "detector": self._manifest_entry(self.detector),
            "classifier": self._manifest_entry(self.classifier),
            "recognizer": self._manifest_entry(self.recognizer),
            "recognitionKeys": (None if self.recognition_keys is None else self._manifest_entry(self.recognition_keys)),
        }
        return ({name: str(path) for name, path in resolved.items()}, canonical_json_sha256(manifest))

    @staticmethod
    def _manifest_entry(model: LocalModelFile) -> dict[str, object]:
        return {
            "relativePath": model.relative_path.replace("\\", "/"),
            "byteSize": model.byte_size,
            "sha256": model.sha256,
        }

    @staticmethod
    def _verify_file(root: Path, model: LocalModelFile) -> Path:
        candidate = root.joinpath(model.relative_path)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            metadata = candidate.lstat()
        except (OSError, ValueError) as error:
            raise BackendInputError("rapidocr", "model_file_outside_root_or_missing") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise BackendInputError("rapidocr", "model_file_must_be_regular_and_non_symlink")
        if metadata.st_size != model.byte_size:
            raise BackendInputError("rapidocr", "model_file_size_mismatch")
        digest = hashlib.sha256()
        try:
            with candidate.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise BackendInputError("rapidocr", "model_file_unreadable") from error
        if f"sha256:{digest.hexdigest()}" != model.sha256:
            raise BackendInputError("rapidocr", "model_file_hash_mismatch")
        return resolved


class RapidOcrAdapter:
    def __init__(
        self,
        engine: _RapidEngine,
        *,
        package_version: str,
        model_id: str,
        array_converter: RasterArrayConverter,
        array_color_order: RasterArrayColorOrder,
        execution_provider: RapidOcrExecutionProvider = RapidOcrExecutionProvider.CPU,
    ) -> None:
        if not package_version or not model_id:
            raise ValueError("RapidOCR package version and model_id must not be empty")
        self._engine = engine
        self._converter = array_converter
        self._array_color_order = array_color_order
        self._execution_provider = execution_provider
        self._identity = BackendIdentity(
            name=f"rapidocr:{array_color_order.value}:{execution_provider.value}",
            version=package_version,
            model=model_id,
        )

    @classmethod
    def from_installed(
        cls,
        *,
        models: RapidOcrLocalModels,
        params: Mapping[str, object],
        array_color_order: RasterArrayColorOrder,
        execution: RapidOcrExecutionProfile,
    ) -> RapidOcrAdapter:
        """Construct with verified local model files; model selection is never inferred."""

        guarded_parameters = {
            "Global.model_root_dir",
            "Global.use_det",
            "Global.use_cls",
            "Global.use_rec",
            "Det.engine_type",
            "Det.model_path",
            "Det.model_dir",
            "Cls.engine_type",
            "Cls.model_path",
            "Cls.model_dir",
            "Rec.engine_type",
            "Rec.model_path",
            "Rec.model_dir",
            "Rec.rec_keys_path",
            "Det.session",
            "Cls.session",
            "Rec.session",
            "EngineConfig.onnxruntime.use_cuda",
            "EngineConfig.onnxruntime.use_dml",
            "EngineConfig.onnxruntime.use_cann",
            "EngineConfig.onnxruntime.use_coreml",
            "EngineConfig.onnxruntime.cuda_ep_cfg.device_id",
        }
        conflicting = sorted(guarded_parameters.intersection(params))
        if conflicting:
            raise BackendInputError("rapidocr", f"guarded_parameters_overridden:{','.join(conflicting)}")
        model_paths, model_id = models.verify()
        try:
            module = cast(_RapidModule, _load_runtime_dependency("rapidocr"))
            numpy = _load_runtime_dependency("numpy")
            package_version = version("rapidocr")
            effective_params = dict(params)
            effective_params.update(
                {
                    "Global.model_root_dir": None,
                    "Global.use_det": True,
                    "Global.use_cls": True,
                    "Global.use_rec": True,
                    "Det.engine_type": module.EngineType.ONNXRUNTIME,
                    "Det.model_path": model_paths["detector"],
                    "Cls.engine_type": module.EngineType.ONNXRUNTIME,
                    "Cls.model_path": model_paths["classifier"],
                    "Rec.engine_type": module.EngineType.ONNXRUNTIME,
                    "Rec.model_path": model_paths["recognizer"],
                    "EngineConfig.onnxruntime.use_cuda": execution.provider is RapidOcrExecutionProvider.CUDA,
                    "EngineConfig.onnxruntime.use_dml": False,
                    "EngineConfig.onnxruntime.use_cann": False,
                    "EngineConfig.onnxruntime.use_coreml": False,
                    "EngineConfig.onnxruntime.cuda_ep_cfg.device_id": execution.device_id,
                }
            )
            if execution.provider is RapidOcrExecutionProvider.CUDA:
                _preload_required_cuda_runtime()
            recognition_keys = model_paths.get("recognitionKeys")
            if recognition_keys is not None:
                effective_params["Rec.rec_keys_path"] = recognition_keys
            engine = module.RapidOCR(params=effective_params)
            if execution.provider is RapidOcrExecutionProvider.CUDA:
                _verify_required_cuda_engine(engine)
        except (ImportError, PackageNotFoundError) as error:
            raise BackendUnavailableError("rapidocr") from error
        except (BackendUnavailableError, BackendExecutionError):
            raise
        except Exception as error:
            raise BackendExecutionError("rapidocr", "initialize") from error

        def convert(image: RasterImage) -> object:
            try:
                array = numpy.frombuffer(image.rgb, dtype=numpy.uint8).reshape((image.height, image.width, 3))
                if array_color_order is RasterArrayColorOrder.BGR:
                    return array[:, :, ::-1].copy()
                return array.copy()
            except Exception as error:
                raise BackendExecutionError("rapidocr", "convert_raster") from error

        return cls(
            engine,
            package_version=package_version,
            model_id=canonical_json_sha256({"models": model_id, "execution": execution.to_json()}),
            array_converter=convert,
            array_color_order=array_color_order,
            execution_provider=execution.provider,
        )

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    def recognize(self, image: RasterImage) -> Sequence[OcrRegion]:
        try:
            output = self._engine(self._converter(image))
            if self._execution_provider is RapidOcrExecutionProvider.CUDA:
                _verify_required_cuda_engine(self._engine)
        except (
            BackendExecutionError,
            BackendOutputError,
            BackendUnavailableError,
        ):
            raise
        except Exception as error:
            raise BackendExecutionError(self._identity.name, "recognize") from error
        boxes = output.boxes
        texts = output.txts
        scores = output.scores
        if boxes is None and texts is None and scores is None:
            return ()
        if boxes is None or texts is None or scores is None:
            raise BackendOutputError(self._identity.name, "boxes, txts, and scores must be present together")
        if len(boxes) != len(texts) or len(texts) != len(scores):
            raise BackendOutputError(self._identity.name, "boxes, txts, and scores lengths differ")
        regions: list[OcrRegion] = []
        for index, (box, text, score) in enumerate(zip(boxes, texts, scores, strict=True)):
            if len(box) != 4 or any(len(point) != 2 for point in box):
                raise BackendOutputError(self._identity.name, f"box {index} is not a four-point polygon")
            if not isinstance(text, str) or not text:
                raise BackendOutputError(self._identity.name, f"text {index} is empty or not a string")
            try:
                polygon = tuple(OcrPoint(float(point[0]), float(point[1])) for point in box)
                if len(polygon) != 4:
                    raise ValueError("polygon length changed during conversion")
                region = OcrRegion(
                    polygon=polygon,
                    text=text,
                    confidence=float(score),
                )
            except (TypeError, ValueError) as error:
                raise BackendOutputError(self._identity.name, f"OCR region {index} contains invalid values") from error
            regions.append(region)
        return tuple(regions)


def _preload_required_cuda_runtime() -> None:
    try:
        onnxruntime = cast(_OnnxRuntimeModule, _load_runtime_dependency("onnxruntime"))
        onnxruntime.preload_dlls()
        available = tuple(onnxruntime.get_available_providers())
        runtime_device = onnxruntime.get_device()
    except ImportError as error:
        raise BackendUnavailableError("rapidocr-cuda") from error
    except Exception as error:
        raise BackendExecutionError("rapidocr-cuda", "preload") from error
    required = "CUDAExecutionProvider"
    if runtime_device.upper() != "GPU" or required not in available:
        raise BackendUnavailableError("rapidocr-cuda")
    _preload_split_cudnn_libraries(onnxruntime)


def _preload_split_cudnn_libraries(onnxruntime: _OnnxRuntimeModule) -> None:
    """Load every DLL from the pinned cuDNN wheel on Windows.

    New cuDNN 9 wheels contain frontend sublibraries that some ONNX Runtime
    preload manifests do not enumerate. Missing one causes a CUDA exception
    followed by ONNX Runtime's automatic CPU retry.
    """

    if platform.system() != "Windows":
        return
    runtime_file = getattr(onnxruntime, "__file__", None)
    if not isinstance(runtime_file, str) or not runtime_file:
        raise BackendUnavailableError("rapidocr-cudnn")
    cudnn_bin = Path(runtime_file).resolve().parent.parent / "nvidia" / "cudnn" / "bin"
    libraries = tuple(sorted(cudnn_bin.glob("cudnn*.dll")))
    if not libraries:
        raise BackendUnavailableError("rapidocr-cudnn")
    with _CUDA_PRELOAD_LOCK:
        if _CUDA_DLL_HANDLES:
            return
        try:
            directory_handle = os.add_dll_directory(str(cudnn_bin))
            handles = [CDLL(str(library)) for library in libraries]
        except (OSError, AttributeError) as error:
            raise BackendExecutionError("rapidocr-cudnn", "preload") from error
        _CUDA_DLL_DIRECTORY_HANDLES.append(directory_handle)
        _CUDA_DLL_HANDLES.extend(handles)


def _verify_required_cuda_engine(engine: _RapidEngine) -> None:
    required = "CUDAExecutionProvider"
    try:
        components = {
            "detector": engine.text_det,
            "classifier": engine.text_cls,
            "recognizer": engine.text_rec,
        }
        for name, component in components.items():
            providers = tuple(component.session.session.get_providers())
            if not providers or providers[0] != required:
                raise BackendUnavailableError(f"rapidocr-cuda-{name}")
    except BackendUnavailableError:
        raise
    except Exception as error:
        raise BackendExecutionError("rapidocr-cuda", "verify_sessions") from error


__all__ = [
    "LocalModelFile",
    "RapidOcrAdapter",
    "RapidOcrExecutionProfile",
    "RapidOcrExecutionProvider",
    "RapidOcrLocalModels",
    "RasterArrayColorOrder",
]
