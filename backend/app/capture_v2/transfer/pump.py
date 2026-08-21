from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureSegment
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.models import RemoteSegmentIdentity
from app.capture_v2.segment.repository import SegmentRepository, utcnow
from app.core.ids import new_id


@dataclass(frozen=True)
class PumpResult:
    sealed: int = 0
    transferred: int = 0
    acked: int = 0
    deleted: int = 0
    errors: int = 0


class ReliableSegmentPump:
    def __init__(self, *, session_factory, sealer, inspector, downloader, persister,
                 acknowledger, temp_root: Path):
        self.session_factory = session_factory
        self.sealer = sealer
        self.inspector = inspector
        self.downloader = downloader
        self.persister = persister
        self.acknowledger = acknowledger
        self.temp_root = Path(temp_root)

    def _epoch(self, epoch_id: str) -> dict:
        with self.session_factory() as db:
            row = db.get(CaptureEpoch, epoch_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_EPOCH_NOT_FOUND")
            return {
                "id": row.id,
                "session": row.capture_session_id,
                "device": row.device_id,
                "token": row.epoch_token,
            }

    def _discover(self, epoch: dict, sealed) -> str:
        with self.session_factory() as db:
            with db.begin():
                repo = SegmentRepository(db)
                row = repo.discover(
                    capture_session_id=epoch["session"], capture_epoch_id=epoch["id"],
                    device_id=epoch["device"], segment_seq=sealed.segment_seq,
                    remote_path=sealed.identity.remote_path, remote_inode=sealed.identity.inode,
                    remote_size=sealed.identity.size, remote_mtime_epoch=sealed.identity.mtime_epoch,
                )
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=epoch["session"],
                    entity_type="CAPTURE_SEGMENT", entity_id=row.id,
                    event_type="SEGMENT_DISCOVERED", source_ts=utcnow(),
                    payload=sealed.identity.as_dict(),
                ))
                return row.id

    def _known_segment_ids(self, capture_epoch_id: str) -> list[str]:
        with self.session_factory() as db:
            rows = db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_epoch_id == capture_epoch_id,
                CaptureSegment.state != "REMOTE_DELETED",
            ).order_by(CaptureSegment.segment_seq)).all()
            return [row.id for row in rows]

    async def _transfer_one(self, segment_id: str, *, token, token_provider=None) -> tuple[int, int, int]:
        repair_acked = False
        with self.session_factory() as db:
            row = SegmentRepository(db).by_id(segment_id)
            if row is None:
                raise CaptureV2Error("SEGMENT_NOT_FOUND")
            ident = RemoteSegmentIdentity(
                row.remote_path, row.remote_inode, row.remote_size, row.remote_mtime_epoch
            )
            if row.state == "REMOTE_DELETED":
                return 0, 1, 1

            server_copy_valid = bool(
                row.storage_key and row.sha256 and row.server_size is not None
                and self.persister.store.verify(
                    storage_key=row.storage_key, size=row.server_size, sha256=row.sha256
                )
            )
            if row.state == "ACKED":
                # ACKED is one-way: never demote the state. But deletion is allowed
                # only while the committed Server object still verifies. If it has
                # disappeared and the exact DUT segment remains, repair the same
                # deterministic Server object first, then continue GC.
                transfer_required = not server_copy_valid
                repair_acked = transfer_required
                if repair_acked:
                    row.last_error_code = "SERVER_COPY_MISSING"
                    row.last_error_detail = {"storage_key": row.storage_key}
                    db.commit()
            else:
                transfer_required = True
                if server_copy_valid:
                    transfer_required = False
                elif row.storage_key and row.sha256 and row.server_size is not None:
                    if row.state in ("PERSISTED", "ACK_PENDING", "ERROR"):
                        row.state = "DISCOVERED"
                        row.last_error_code = "SERVER_COPY_MISSING"
                        row.last_error_detail = {"storage_key": row.storage_key}
                        db.commit()
                if row.state == "ERROR" and row.last_error_code != "SERVER_COPY_MISSING":
                    row.state = "DISCOVERED"
                    db.commit()

        transferred = 0
        if transfer_required:
            with self.session_factory() as db:
                row = SegmentRepository(db).by_id(segment_id)
                if row is None:
                    raise CaptureV2Error("SEGMENT_NOT_FOUND")
                state = row.state
            if state in ("DISCOVERED", "TRANSFERRING", "DOWNLOADED", "VERIFIED", "PERSISTING", "ACKED"):
                before = await self.inspector.stat(ident.remote_path)
                if before.inode != ident.inode or before.size != ident.size:
                    raise CaptureV2Error("SEGMENT_IDENTITY_CONFLICT")
                local = self.temp_root / f"{segment_id}.pcap.part"
                if not repair_acked:
                    with self.session_factory() as db:
                        with db.begin():
                            repo = SegmentRepository(db)
                            row = repo.by_id(segment_id)
                            if row.state in ("DISCOVERED", "ERROR"):
                                repo.transition(
                                    row.id, expected=row.state, next_state="TRANSFERRING",
                                    transfer_attempts=int(row.transfer_attempts or 0) + 1,
                                    download_started_at=utcnow(), local_temp_path=str(local),
                                )
                await self.downloader.get(remote_path=ident.remote_path, local_path=local)
                after = await self.inspector.stat(ident.remote_path)
                if after.inode != ident.inode or after.size != ident.size:
                    raise CaptureV2Error("SEGMENT_CHANGED_DURING_TRANSFER")
                if local.stat().st_size != ident.size:
                    raise CaptureV2Error("SEGMENT_SIZE_MISMATCH")
                if repair_acked:
                    self.persister.repair_durable_copy(segment_id, local)
                else:
                    with self.session_factory() as db:
                        with db.begin():
                            repo = SegmentRepository(db)
                            row = repo.by_id(segment_id)
                            if row.state == "TRANSFERRING":
                                repo.transition(
                                    row.id, expected="TRANSFERRING", next_state="DOWNLOADED",
                                    downloaded_at=utcnow(),
                                )
                    self.persister.persist(segment_id, local)
                transferred = 1

        with self.session_factory() as db:
            with db.begin():
                repo = SegmentRepository(db)
                row = repo.by_id(segment_id)
                if row is None:
                    raise CaptureV2Error("SEGMENT_NOT_FOUND")
                if row.state == "PERSISTED":
                    row = repo.transition(
                        row.id, expected="PERSISTED", next_state="ACK_PENDING",
                        ack_pending_at=utcnow(),
                    )
                state_after_persist = row.state

        if state_after_persist == "ACK_PENDING":
            use_token = token_provider() if token_provider else token
            with self.session_factory() as db:
                with db.begin():
                    row = SegmentRepository(db).by_id(segment_id)
                    SegmentRepository(db).transition(
                        row.id, expected="ACK_PENDING", next_state="ACKED",
                        acked_at=utcnow(), lease_epoch_at_ack=use_token.lease_epoch,
                    )
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=row.capture_session_id,
                        entity_type="CAPTURE_SEGMENT", entity_id=row.id,
                        event_type="SEGMENT_ACKED", source_ts=utcnow(),
                        payload={"lease_epoch": use_token.lease_epoch},
                    ))
            acked = 1
        else:
            acked = 1 if state_after_persist in ("ACKED", "REMOTE_DELETED") else 0

        with self.session_factory() as db:
            row = SegmentRepository(db).by_id(segment_id)
            if row is None:
                raise CaptureV2Error("SEGMENT_NOT_FOUND")
            state_before_delete = row.state
            server_copy_valid_before_delete = bool(
                row.storage_key and row.sha256 and row.server_size is not None
                and self.persister.store.verify(
                    storage_key=row.storage_key, size=row.server_size, sha256=row.sha256
                )
            )
        deleted = 1 if state_before_delete == "REMOTE_DELETED" else 0
        if state_before_delete == "ACKED":
            # Never issue remote DELETE unless the committed Server object verifies
            # immediately before the fenced mutation. This protects against a
            # durable-store loss between ACK and delayed GC.
            if not server_copy_valid_before_delete:
                raise CaptureV2Error("SERVER_COPY_MISSING")
            use_token = token_provider() if token_provider else token
            try:
                await self.acknowledger.delete_remote(use_token, ident)
            except CaptureV2Error as exc:
                if exc.code in ("LEASE_FENCED", "LEASE_EXPIRED_LOCAL", "PRODUCER_IDENTITY_MISMATCH"):
                    raise
                with self.session_factory() as db:
                    with db.begin():
                        current = SegmentRepository(db).by_id(segment_id)
                        current.last_error_code = "REMOTE_DELETE_PENDING"
                        current.last_error_detail = {"cause": exc.code}
                return transferred, acked, 0
            with self.session_factory() as db:
                with db.begin():
                    row = SegmentRepository(db).transition(
                        segment_id, expected="ACKED", next_state="REMOTE_DELETED",
                        remote_deleted_at=utcnow(),
                    )
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=row.capture_session_id,
                        entity_type="CAPTURE_SEGMENT", entity_id=row.id,
                        event_type="SEGMENT_REMOTE_DELETED", source_ts=utcnow(), payload={},
                    ))
            deleted = 1
        return transferred, acked, deleted

    async def run_once(self, *, capture_epoch_id: str, token, producer_pid: int,
                       producer_starttime: int, token_provider=None) -> PumpResult:
        epoch = self._epoch(capture_epoch_id)
        sealed = await self.sealer.seal_closed(
            token, capture_epoch=epoch["token"], producer_pid=producer_pid,
            producer_starttime=producer_starttime,
        )
        newly_discovered = [self._discover(epoch, item) for item in sealed]
        ids = list(dict.fromkeys(self._known_segment_ids(capture_epoch_id) + newly_discovered))
        transferred = acked = deleted = errors = 0
        for sid in ids:
            try:
                t, a, d = await self._transfer_one(sid, token=token, token_provider=token_provider)
                transferred += t
                acked += a
                deleted += d
            except CaptureV2Error as exc:
                errors += 1
                with self.session_factory() as db:
                    with db.begin():
                        try:
                            SegmentRepository(db).set_error(sid, exc.code, exc.details)
                        except Exception:
                            pass
        return PumpResult(len(sealed), transferred, acked, deleted, errors)

    async def run_final_once(self, *, capture_epoch_id: str, token, producer_pid: int,
                             producer_starttime: int, token_provider=None) -> PumpResult:
        """Drain the final OPEN file after the exact producer identity has stopped."""
        epoch = self._epoch(capture_epoch_id)
        sealed = await self.sealer.seal_all_after_stop(
            token, capture_epoch=epoch["token"], producer_pid=producer_pid,
            producer_starttime=producer_starttime,
        )
        newly_discovered = [self._discover(epoch, item) for item in sealed]
        ids = list(dict.fromkeys(self._known_segment_ids(capture_epoch_id) + newly_discovered))
        transferred = acked = deleted = errors = 0
        for sid in ids:
            try:
                t, a, d = await self._transfer_one(sid, token=token, token_provider=token_provider)
                transferred += t; acked += a; deleted += d
            except CaptureV2Error as exc:
                errors += 1
                with self.session_factory() as db:
                    with db.begin():
                        try:
                            SegmentRepository(db).set_error(sid, exc.code, exc.details)
                        except Exception:
                            pass
        return PumpResult(len(sealed), transferred, acked, deleted, errors)
