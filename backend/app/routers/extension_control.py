from fastapi import APIRouter, Depends, Query

from fastapi import HTTPException
from app.core.auth_session import decode_session_token, get_bearer_token
from app.schemas_extension import (
    CommandAckRequest,
    CommandResultRequest,
    ExecutionBatchRequest,
    HeartbeatRequest,
    PairCompleteRequest,
    PairStartRequest,
    PlatformSessionsUpsertRequest,
    StateSyncRequest,
)
from app.services.extension_control_plane import (
    ack_execution_command,
    complete_pairing,
    create_execution_batch,
    heartbeat_extension,
    ingest_execution_result,
    ingest_state_sync,
    poll_execution_commands,
    start_pairing,
    upsert_platform_session,
)

router = APIRouter(tags=["extension-control"])


def get_required_telegram_user_id(token: str | None = Depends(get_bearer_token)) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Missing authenticated session")
    payload = decode_session_token(token)
    return str(payload["sub"])


@router.post("/extension/pair/start")
async def extension_pair_start(
    payload: PairStartRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    return await start_pairing(user_id=user_id, device_label=payload.device_label, metadata=payload.metadata)


@router.post("/extension/pair/complete")
async def extension_pair_complete(payload: PairCompleteRequest):
    return await complete_pairing(payload.pair_code, payload.pair_secret, payload.model_dump())


@router.post("/extension/heartbeat")
async def extension_heartbeat(
    payload: HeartbeatRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    return await heartbeat_extension(user_id, payload.extension_device_id, payload.model_dump())


@router.post("/extension/platform-sessions/upsert")
async def extension_upsert_platform_sessions(
    payload: PlatformSessionsUpsertRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    rows = await upsert_platform_session(user_id, payload.extension_device_id, [s.model_dump() for s in payload.sessions])
    return {"ok": True, "platform_sessions": rows}


@router.post("/extension/state-sync")
async def extension_state_sync(
    payload: StateSyncRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    return await ingest_state_sync(user_id, payload.extension_device_id, payload.model_dump())


@router.post("/execution/batches")
async def create_batch(
    payload: ExecutionBatchRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    return await create_execution_batch(user_id, payload.model_dump())


@router.get("/execution/commands/poll")
async def poll_commands(
    extension_device_id: int = Query(...),
    adapter_keys: list[str] | None = Query(default=None),
    user_id: str = Depends(get_required_telegram_user_id),
):
    return {"commands": await poll_execution_commands(user_id, extension_device_id, adapter_keys=adapter_keys)}


@router.post("/execution/commands/{command_id}/ack")
async def command_ack(
    command_id: int,
    payload: CommandAckRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    return await ack_execution_command(user_id, command_id, payload.status, metadata=payload.metadata)


@router.post("/execution/commands/{command_id}/result")
async def command_result(
    command_id: int,
    payload: CommandResultRequest,
    user_id: str = Depends(get_required_telegram_user_id),
):
    return await ingest_execution_result(user_id, command_id, payload.model_dump())
