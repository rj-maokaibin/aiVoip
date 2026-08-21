from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _existing_models  # noqa
from app.db.base import Base
from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureSegment, CaptureSession
from app.capture_v2.enums import CaptureEpochState, CaptureHealth, CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.models import RemoteSegmentIdentity
from app.capture_v2.segment.sealer import SealedRemoteSegment
from app.capture_v2.storage.local import LocalDurableSegmentStore
from app.capture_v2.transfer.persister import SegmentPersister
from app.capture_v2.transfer.pump import ReliableSegmentPump


def _factory():
    engine=create_engine('sqlite+pysqlite:///:memory:',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine,tables=[CaptureSession.__table__,CaptureEpoch.__table__,CaptureEvent.__table__,CaptureSegment.__table__])
    F=sessionmaker(bind=engine,expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(id='S',reproduction_session_id='R',device_id='D',state=CaptureSessionState.PREPARING.value,
            health_status=CaptureHealth.HEALTHY.value,capture_profile_id='p',capture_profile_version='1',platform_profile_id='mt7621',platform_profile_version='1',effective_profile={}))
        db.add(CaptureEpoch(id='E',capture_session_id='S',device_id='D',epoch_index=1,epoch_token='CAP_E',lease_epoch_started=1,
            state=CaptureEpochState.RUNNING.value,producer_pid=10,producer_starttime=20,interface='br-lan_400'))
    return F


def _pcap_bytes():
    return struct.pack('<IHHIIII',0xA1B2C3D4,2,4,0,0,65535,1)


class Sealer:
    async def seal_closed(self,*args,**kwargs):
        return (SealedRemoteSegment(1,RemoteSegmentIdentity('/tmp/seg1.pcap',123,24,1)),)
class Inspector:
    async def stat(self,path): return RemoteSegmentIdentity(path,123,24,1)
class Downloader:
    async def get(self,*,remote_path,local_path,timeout=None): local_path.parent.mkdir(parents=True,exist_ok=True); local_path.write_bytes(_pcap_bytes())
class Ack:
    def __init__(self,fail=None): self.fail=fail; self.calls=0
    async def delete_remote(self,token,identity):
        self.calls+=1
        if self.fail: raise CaptureV2Error(self.fail)


def _token():
    return SimpleNamespace(lease_epoch=1,expires_at=datetime.now(timezone.utc)+timedelta(seconds=30))


def test_normal_segment_reaches_remote_deleted_and_server_copy_is_durable(tmp_path):
    F=_factory(); store=LocalDurableSegmentStore(tmp_path/'store'); pers=SegmentPersister(F,store); ack=Ack()
    pump=ReliableSegmentPump(session_factory=F,sealer=Sealer(),inspector=Inspector(),downloader=Downloader(),persister=pers,
        acknowledger=ack,temp_root=tmp_path/'tmp')
    result=asyncio.run(pump.run_once(capture_epoch_id='E',token=_token(),producer_pid=10,producer_starttime=20))
    assert (result.transferred,result.acked,result.deleted,result.errors)==(1,1,1,0)
    with F() as db:
        row=db.query(CaptureSegment).one(); assert row.state=='REMOTE_DELETED'; assert row.sha256; assert row.storage_key
        assert store.verify(storage_key=row.storage_key,size=row.server_size,sha256=row.sha256)


def test_fenced_remote_delete_never_demotes_acked_server_evidence(tmp_path):
    F=_factory(); store=LocalDurableSegmentStore(tmp_path/'store'); pers=SegmentPersister(F,store); ack=Ack('LEASE_FENCED')
    pump=ReliableSegmentPump(session_factory=F,sealer=Sealer(),inspector=Inspector(),downloader=Downloader(),persister=pers,
        acknowledger=ack,temp_root=tmp_path/'tmp')
    result=asyncio.run(pump.run_once(capture_epoch_id='E',token=_token(),producer_pid=10,producer_starttime=20))
    assert result.errors==1
    with F() as db:
        row=db.query(CaptureSegment).one(); assert row.state=='ACKED'; assert row.last_error_code=='LEASE_FENCED'
        assert store.verify(storage_key=row.storage_key,size=row.server_size,sha256=row.sha256)

def test_acked_missing_server_copy_is_repaired_from_exact_dut_segment_before_delete(tmp_path):
    F=_factory(); store=LocalDurableSegmentStore(tmp_path/'store'); pers=SegmentPersister(F,store); ack=Ack('REMOTE_DELETE_IO')
    pump=ReliableSegmentPump(session_factory=F,sealer=Sealer(),inspector=Inspector(),downloader=Downloader(),persister=pers,
        acknowledger=ack,temp_root=tmp_path/'tmp')
    first=asyncio.run(pump.run_once(capture_epoch_id='E',token=_token(),producer_pid=10,producer_starttime=20))
    assert first.acked==1 and first.deleted==0
    with F() as db:
        row=db.query(CaptureSegment).one(); assert row.state=='ACKED'
        server_path = store.root / row.storage_key
        assert server_path.is_file()
    server_path.unlink()
    assert not server_path.exists()
    ack.fail=None
    second=asyncio.run(pump.run_once(capture_epoch_id='E',token=_token(),producer_pid=10,producer_starttime=20))
    assert second.errors==0
    assert second.transferred==1  # exact DUT segment was re-fetched for repair
    assert second.deleted==1
    with F() as db:
        row=db.query(CaptureSegment).one(); assert row.state=='REMOTE_DELETED'
        assert row.last_error_code is None
        assert store.verify(storage_key=row.storage_key,size=row.server_size,sha256=row.sha256)
