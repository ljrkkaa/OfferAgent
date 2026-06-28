import asyncio
import json
import logging
import math
from typing import Dict, List, Optional, Union

from asgiref.sync import sync_to_async
from fastapi import (
    APIRouter,
    Depends,
    Header,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel
from starlette.authentication import requires

from khoj.database.adapters import (
    EntryAdapters,
    FileObjectAdapters,
)
from khoj.processor.content.pdf.pdf_to_entries import PdfToEntries
from khoj.routers.helpers import (
    ApiIndexedDataLimiter,
    CommonQueryParams,
    configure_content,
    get_file_content,
    update_telemetry_state,
)
from khoj.utils import state
from khoj.utils.state import SearchType

logger = logging.getLogger(__name__)

api_content = APIRouter()


class IndexerInput(BaseModel):
    markdown: Optional[dict[str, str]] = None
    pdf: Optional[dict[str, bytes]] = None
    plaintext: Optional[dict[str, str]] = None


@api_content.put("")
@requires(["authenticated"])
async def put_content(
    request: Request,
    files: List[UploadFile] = [],
    t: Optional[Union[state.SearchType, str]] = state.SearchType.All,
    client: Optional[str] = None,
    user_agent: Optional[str] = Header(None),
    referer: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
    indexed_data_limiter: ApiIndexedDataLimiter = Depends(
        ApiIndexedDataLimiter(
            incoming_entries_size_limit=50,
            subscribed_incoming_entries_size_limit=100,
            total_entries_size_limit=50,
            subscribed_total_entries_size_limit=500,
        )
    ),
):
    return await indexer(request, files, t, True, client, user_agent, referer, host)


@api_content.patch("")
@requires(["authenticated"])
async def patch_content(
    request: Request,
    files: List[UploadFile] = [],
    t: Optional[Union[state.SearchType, str]] = state.SearchType.All,
    client: Optional[str] = None,
    user_agent: Optional[str] = Header(None),
    referer: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
    indexed_data_limiter: ApiIndexedDataLimiter = Depends(
        ApiIndexedDataLimiter(
            incoming_entries_size_limit=50,
            subscribed_incoming_entries_size_limit=100,
            total_entries_size_limit=50,
            subscribed_total_entries_size_limit=500,
        )
    ),
):
    return await indexer(request, files, t, False, client, user_agent, referer, host)


@api_content.delete("/file", status_code=201)
@requires(["authenticated"])
async def delete_content_files(
    request: Request,
    filename: str,
    client: Optional[str] = None,
):
    user = request.user.object

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="delete_file",
        client=client,
    )

    await EntryAdapters.adelete_entry_by_file(user, filename)

    await FileObjectAdapters.adelete_file_object_by_name(user, filename)

    return {"status": "ok"}


class DeleteFilesRequest(BaseModel):
    files: List[str]


@api_content.delete("/files", status_code=201)
@requires(["authenticated"])
async def delete_content_file(
    request: Request,
    files: DeleteFilesRequest,
    client: Optional[str] = None,
):
    user = request.user.object

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="delete_file",
        client=client,
    )

    deleted_count = await EntryAdapters.adelete_entries_by_filenames(user, files.files)
    for file in files.files:
        await FileObjectAdapters.adelete_file_object_by_name(user, file)

    return {"status": "ok", "deleted_count": deleted_count}


@api_content.get("/size", response_model=Dict[str, int])
@requires(["authenticated"])
async def get_content_size(request: Request, common: CommonQueryParams, client: Optional[str] = None):
    user = request.user.object
    indexed_data_size_in_mb = await sync_to_async(EntryAdapters.get_size_of_indexed_data_in_mb)(user)
    return Response(
        content=json.dumps({"indexed_data_size_in_mb": math.ceil(indexed_data_size_in_mb)}),
        media_type="application/json",
        status_code=200,
    )


@api_content.get("/types", response_model=List[str])
@requires(["authenticated"])
def get_content_types(request: Request, client: Optional[str] = None):
    user = request.user.object
    all_content_types = {s.value for s in SearchType}
    configured_content_types = set(EntryAdapters.get_unique_file_types(user))
    configured_content_types |= {"all"}

    return list(configured_content_types & all_content_types)


@api_content.get("/files", response_model=Dict[str, str])
@requires(["authenticated"])
async def get_all_files(
    request: Request, client: Optional[str] = None, truncated: Optional[bool] = True, page: int = 0
):
    user = request.user.object

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="get_all_filenames",
        client=client,
    )

    files_data = []
    page_size = 10

    file_objects = await FileObjectAdapters.aget_all_file_objects(user, start=page * page_size, limit=page_size)

    num_pages = await FileObjectAdapters.aget_number_of_pages(user, page_size)

    for file_object in file_objects:
        files_data.append(
            {
                "file_name": file_object.file_name,
                "raw_text": file_object.raw_text[:1000] if truncated else file_object.raw_text,
                "updated_at": str(file_object.updated_at),
            }
        )

    data_packet = {
        "files": files_data,
        "num_pages": num_pages,
    }

    return Response(content=json.dumps(data_packet), media_type="application/json", status_code=200)


@api_content.get("/file", response_model=Dict[str, str])
@requires(["authenticated"])
async def get_file_object(
    request: Request,
    file_name: str,
    client: Optional[str] = None,
):
    user = request.user.object

    file_object = (await FileObjectAdapters.aget_file_objects_by_name(user, file_name))[0]
    if not file_object:
        return Response(
            content=json.dumps({"error": "File not found"}),
            media_type="application/json",
            status_code=404,
        )

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="get_file",
        client=client,
    )

    return Response(
        content=json.dumps(
            {"id": file_object.id, "file_name": file_object.file_name, "raw_text": file_object.raw_text}
        ),
        media_type="application/json",
        status_code=200,
    )


@api_content.delete("/type/{content_type}", status_code=200)
@requires(["authenticated"])
async def delete_content_type(
    request: Request,
    content_type: str,
    client: Optional[str] = None,
):
    user = request.user.object
    if content_type not in {s.value for s in SearchType}:
        raise ValueError(f"Unsupported content type: {content_type}")
    if content_type == "all":
        await EntryAdapters.adelete_all_entries(user)
    else:
        # Delete file objects of the given type
        file_list = await sync_to_async(list)(EntryAdapters.get_all_filenames_by_type(user, content_type))  # type: ignore[call-arg]
        await FileObjectAdapters.adelete_file_objects_by_names(user, file_list)
        # Delete entries of the given type
        await EntryAdapters.adelete_all_entries(user, file_type=content_type)

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="delete_content_config",
        client=client,
        metadata={"content_type": content_type},
    )

    return {"status": "ok"}


@api_content.delete("/source/{content_source}", status_code=200)
@requires(["authenticated"])
async def delete_content_source(
    request: Request,
    content_source: str,
    client: Optional[str] = None,
):
    user = request.user.object

    if content_source != "computer":
        raise ValueError(f"Invalid content source: {content_source}")

    file_list = await sync_to_async(list)(EntryAdapters.get_all_filenames_by_source(user, content_source))  # type: ignore[call-arg]
    await FileObjectAdapters.adelete_file_objects_by_names(user, file_list)
    # Delete entries from the given source
    await EntryAdapters.adelete_all_entries(user, file_source=content_source)

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="delete_content_config",
        client=client,
        metadata={"content_source": content_source},
    )

    return {"status": "ok"}


@api_content.post("/convert", status_code=200)
@requires(["authenticated"])
async def convert_documents(
    request: Request,
    files: List[UploadFile],
    client: Optional[str] = None,
):
    MAX_FILE_SIZE_MB = 10  # 10MB limit
    MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

    converted_files = []
    supported_files = ["markdown", "pdf", "plaintext"]

    for file in files:
        # Check file size first
        file_size = 0
        content = await file.read()
        file_size = len(content)
        await file.seek(0)  # Reset file pointer

        if file_size > MAX_FILE_SIZE_BYTES:
            logger.warning(
                f"Skipped converting oversized file ({file_size / 1024 / 1024:.1f}MB) sent by {client} client: {file.filename}"
            )
            continue

        file_data = get_file_content(file)
        if file_data.file_type in supported_files:
            extracted_content = (
                file_data.content.decode(file_data.encoding) if file_data.encoding else file_data.content
            )

            if file_data.file_type == "pdf":
                entries_per_page = PdfToEntries.extract_text(file_data.content)
                annotated_pages = [
                    f"Page {index} of {file_data.name}:\n\n{entry}" for index, entry in enumerate(entries_per_page)
                ]
                extracted_content = "\n".join(annotated_pages)
            elif isinstance(extracted_content, bytes):
                extracted_content = extracted_content.decode("utf-8")

            # Calculate size in bytes. Some of the content might be in bytes, some in str.
            if isinstance(extracted_content, str):
                size_in_bytes = len(extracted_content.encode("utf-8"))
            elif isinstance(extracted_content, bytes):
                size_in_bytes = len(extracted_content)
            else:
                size_in_bytes = 0
                logger.warning(f"Unexpected content type: {type(extracted_content)}")

            converted_files.append(
                {
                    "name": file_data.name,
                    "content": extracted_content,
                    "file_type": file_data.file_type,
                    "size": size_in_bytes,
                }
            )
        else:
            logger.warning(f"Skipped converting unsupported file type sent by {client} client: {file.filename}")

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="convert_documents",
        client=client,
    )

    return Response(content=json.dumps(converted_files), media_type="application/json", status_code=200)


async def indexer(
    request: Request,
    files: list[UploadFile],
    t: Optional[Union[state.SearchType, str]] = state.SearchType.All,
    regenerate: bool = False,
    client: Optional[str] = None,
    user_agent: Optional[str] = Header(None),
    referer: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
):
    user = request.user.object
    method = "regenerate" if regenerate else "sync"
    index_files: Dict[str, Dict[str, str]] = {
        "markdown": {},
        "pdf": {},
        "plaintext": {},
    }
    try:
        logger.info(f"📬 Updating content index via API call by {client} client")
        for file in files:
            file_data = get_file_content(file)
            if file_data.file_type in index_files:
                index_files[file_data.file_type][file_data.name] = (
                    file_data.content.decode(file_data.encoding) if file_data.encoding else file_data.content
                )
            else:
                logger.debug(f"Skipped indexing unsupported file type sent by {client} client: {file_data.name}")

        indexer_input = IndexerInput(
            markdown=index_files["markdown"],
            pdf=index_files["pdf"],
            plaintext=index_files["plaintext"],
        )

        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None,
            configure_content,
            user,
            indexer_input.model_dump(),
            regenerate,
            t,
        )
        if not success:
            raise RuntimeError(f"Failed to {method} {t} data sent by {client} client into content index")
        logger.info(f"Finished {method} {t} data sent by {client} client into content index")
    except Exception as e:
        logger.error(f"Failed to {method} {t} data sent by {client} client into content index: {e}", exc_info=True)
        logger.error(
            f"🚨 Failed to {method} {t} data sent by {client} client into content index: {e}",
            exc_info=True,
        )
        return Response(content="Failed", status_code=500)

    indexing_metadata = {
        "num_markdown": len(index_files["markdown"]),
        "num_pdf": len(index_files["pdf"]),
        "num_plaintext": len(index_files["plaintext"]),
    }

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="index/update",
        client=client,
        user_agent=user_agent,
        referer=referer,
        host=host,
        metadata=indexing_metadata,
    )

    logger.info(f"📪 Content index updated via API call by {client} client")

    indexed_filenames = ",".join(file for ctype in index_files for file in index_files[ctype]) or ""
    return Response(content=indexed_filenames, status_code=200)
