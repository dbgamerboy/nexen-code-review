"""Isolated H: fixtures; no real model, provider, source execution or paid calls."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import app_auth
import continuity_worker as module


class DB:
    def __init__(self, path):
        """Initialize the DB instance."""
        self.path=path
    @contextmanager
    def connect(self):
        """Perform the connect operation."""
        c=sqlite3.connect(self.path, timeout=10);c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally: c.close()
    def rows(self, sql, args=()):
        """Perform the rows operation."""
        with self.connect() as c: return [dict(row) for row in c.execute(sql,args)]


class Mode:
    reason='ready'
    def gate(self):
        """Perform the gate operation."""
        return self.reason


class FakeKilo:
    def __init__(self, db):
        """Initialize the FakeKilo instance."""
        self.db=db;self.active=None;self.calls=[];self.records=[];self.results={};self.worker=None
        self.delay=.025;self.outcome='draft_ready';self.busy=False;self.cancelled=[]
        with db.connect() as c:c.execute('CREATE TABLE kilo_draft_jobs(id TEXT PRIMARY KEY,task_id INTEGER,status TEXT,created_at TEXT)')
    def add(self, n, status='prepared', attempts=0):
        """Perform the add operation."""
        ident=f'{n:032x}'
        self.results[ident]={'id':ident,'task_id':n,'status':status,'attempts':attempts}
        with self.db.connect() as c:c.execute('INSERT INTO kilo_draft_jobs VALUES(?,?,?,?)',(ident,n,status,f'{n:04}'))
        return ident
    def get(self, ident):
        """Handle a GET request."""
        return dict(self.results[ident])
    def record(self, receipt):
        """Record the operation."""
        self.records.append(receipt['id']);self.results[receipt['id']]=dict(receipt)
        with self.db.connect() as c:c.execute('UPDATE kilo_draft_jobs SET status=? WHERE id=?',(receipt['status'],receipt['id']))
    async def start(self, ident):
        """Start the operation."""
        if self.busy or self.active:raise HTTPException(409,'Fixture busy')
        self.calls.append(ident);self.active=ident
        receipt=self.get(ident);receipt.update(status='running',attempts=1);self.record(receipt)
        async def finish():
            await asyncio.sleep(self.delay)
            receipt=self.get(ident);receipt['status']='cancelled' if ident in self.cancelled else self.outcome
            self.record(receipt);self.active=None
        self.worker=asyncio.create_task(finish())
        return self.get(ident)
    def cancel(self,ident):
        """Cancel the active operation."""
        self.cancelled.append(ident)


class ContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Perform the asyncSetUp operation."""
        self.temp=tempfile.TemporaryDirectory(dir='H:/NEXEN/temp');self.root=Path(self.temp.name)
        self.db=DB(self.root/'tasks.sqlite3');self.kilo=FakeKilo(self.db);self.mode=Mode();self.workers=[]
        (self.root/'kilo').mkdir();(self.root/'data').mkdir()
        self.worker=self.make()
    def make(self):
        """Create the operation."""
        w=module.ContinuityWorker(self.db,self.kilo,self.mode,state_dir=self.root/'state',kilo_root=self.root/'kilo',base=self.root,interval=.05)
        self.workers.append(w);return w
    async def asyncTearDown(self):
        """Perform the asyncTearDown operation."""
        for w in self.workers:
            await w.shutdown()
            if w.lease:w.lease.__exit__();w.lease=None
        self.temp.cleanup()
    def own_without_loop(self,w=None):
        """Perform the own without loop operation."""
        w=w or self.worker;w.lease=module.SingleWriter(w.state_dir);w.lease.__enter__()
    async def test_one_job_per_cycle_success_no_original_task_completion(self):
        """Verify one job per cycle success no original task completion."""
        one=self.kilo.add(1);two=self.kilo.add(2);self.own_without_loop();self.worker.set_enabled(True)
        self.assertEqual(await self.worker.cycle(),'draft_ready');self.assertEqual(self.kilo.calls,[one])
        self.assertEqual(self.kilo.get(two)['status'],'prepared')
        receipt=self.worker.status()['receipts'][0]
        self.assertEqual((receipt['job_id'],receipt['task_id'],receipt['attempts']),(one,1,1))
        self.assertEqual(await self.worker.cycle(),'draft_ready');self.assertEqual(self.kilo.calls,[one,two])
        self.assertEqual(await self.worker.cycle(),'waiting_for_prepared_job')
        self.assertFalse(self.worker.status()['cloud']['enabled'])
    async def test_failed_job_never_retried_or_reprompted(self):
        """Verify failed job never retried or reprompted."""
        one=self.kilo.add(1);self.kilo.outcome='failed';self.own_without_loop();self.worker.set_enabled(True)
        self.assertEqual(await self.worker.cycle(),'failed')
        for _ in range(3):self.assertEqual(await self.worker.cycle(),'waiting_for_prepared_job')
        self.assertEqual(self.kilo.calls,[one]);self.assertEqual(self.worker.status()['counts']['failed'],1)
        self.worker.set_enabled(False);self.worker.set_enabled(True)
        self.assertEqual(await self.worker.cycle(),'waiting_for_prepared_job')
    async def test_durable_pause_and_external_controls_are_preserved(self):
        """Verify durable pause and external controls are preserved."""
        self.kilo.add(1);self.own_without_loop();self.worker.set_enabled(True)
        marker=self.root/'data/PAUSE_AUTONOMY';marker.write_text('owned by user')
        self.assertEqual(await self.worker.cycle(),'external_pause');self.assertEqual(marker.read_text(),'owned by user')
        marker.unlink();self.mode.reason='maintenance'
        self.assertEqual(await self.worker.cycle(),'automatic_maintenance');self.assertEqual(self.kilo.calls,[])
        self.mode.reason='ready';self.worker.set_enabled(False)
        self.assertFalse(self.make().settings()['enabled']);self.assertEqual(await self.worker.cycle(),'paused')
    async def test_pause_during_job_finishes_only_current_job(self):
        """Verify pause during job finishes only current job."""
        one=self.kilo.add(1);self.kilo.add(2);self.kilo.delay=.08;self.own_without_loop();self.worker.set_enabled(True)
        job=asyncio.create_task(self.worker.cycle())
        while self.kilo.active is None:await asyncio.sleep(.005)
        self.worker.set_enabled(False);self.assertEqual(await job,'draft_ready')
        self.assertEqual(await self.worker.cycle(),'paused');self.assertEqual(self.kilo.calls,[one]);self.assertEqual(self.kilo.cancelled,[])
    async def test_singleton_and_empty_queue_wait_without_repeated_model_calls(self):
        """Verify singleton and empty queue wait without repeated model calls."""
        self.worker.set_enabled(True);self.assertTrue(await self.worker.start())
        self.assertFalse(await self.worker.start());other=self.make();self.assertFalse(await other.start())
        await asyncio.sleep(.12);self.assertEqual(self.worker.health,'waiting_for_prepared_job');self.assertEqual(self.kilo.calls,[])
        self.assertTrue((self.root/'state/status.json').exists())
    async def test_busy_adapter_defers_without_consuming_attempt(self):
        """Verify busy adapter defers without consuming attempt."""
        ident=self.kilo.add(1);self.kilo.busy=True;self.own_without_loop();self.worker.set_enabled(True)
        self.assertEqual(await self.worker.cycle(),'waiting_for_existing_kilo')
        self.assertEqual(self.db.rows('SELECT * FROM continuity_receipts'),[])
        self.kilo.busy=False;self.assertEqual(await self.worker.cycle(),'draft_ready');self.assertEqual(self.kilo.calls,[ident])
    async def test_recovery_only_own_running_claims_and_respects_live_lock(self):
        """Verify recovery only own running claims and respects live lock."""
        own=self.kilo.add(1,'running',1);unrelated=self.kilo.add(2,'running',1);claimed=self.kilo.add(3,'running',1)
        with self.db.connect() as c:
            for ident,state,attempt in [(own,'running',1),(claimed,'claimed',0)]:
                c.execute('INSERT INTO continuity_receipts(job_id,task_id,owner,state,attempts,claimed_at) VALUES(?,?,?,?,?,?)',(ident,1,'oldowner',state,attempt,module.timestamp()))
        self.own_without_loop()
        with module.SingleWriter(self.root/'kilo'):
            self.assertFalse(self.worker.recover_owned());self.assertEqual(self.kilo.records,[])
        self.assertTrue(self.worker.recover_owned());self.assertEqual(self.kilo.get(own)['status'],'interrupted')
        self.assertEqual(self.kilo.get(unrelated)['status'],'running');self.assertEqual(self.kilo.get(claimed)['status'],'running')
        self.assertEqual(self.kilo.records,[own]);self.assertTrue(self.worker.recover_owned());self.assertEqual(self.kilo.records,[own])
    async def test_nonfresh_job_and_fault_never_retry(self):
        """Verify nonfresh job and fault never retry."""
        ident=self.kilo.add(1,attempts=1);self.own_without_loop();self.worker.set_enabled(True)
        self.assertEqual(await self.worker.cycle(),'skipped_nonfresh_job');self.assertEqual(self.kilo.calls,[])
        self.assertEqual(await self.worker.cycle(),'waiting_for_prepared_job')
        self.assertEqual(self.worker.status()['receipts'][0]['job_id'],ident)
    async def test_loop_fault_pauses_durably_without_logging_prompt(self):
        """Verify loop fault pauses durably without logging prompt."""
        self.worker.set_enabled(True)
        with patch.object(self.worker,'cycle',side_effect=ValueError('PRIVATE FIXTURE CONTENT')):
            await self.worker.start();await asyncio.sleep(.02);await self.worker.shutdown()
        self.assertFalse(self.worker.settings()['enabled'])
        self.assertNotIn('PRIVATE FIXTURE CONTENT',(self.root/'state/worker.log').read_text())
        self.assertIn('ValueError',self.worker.settings()['last_error'])
    async def test_shutdown_cancels_only_owned_active_job(self):
        """Verify shutdown cancels only owned active job."""
        one=self.kilo.add(1);self.kilo.delay=.08;self.worker.set_enabled(True);await self.worker.start()
        while self.kilo.active is None:await asyncio.sleep(.005)
        await self.worker.shutdown();self.assertEqual(self.kilo.cancelled,[one]);self.assertFalse(self.worker.lease)

    async def test_shutdown_cancel_failure_still_awaits_loop_and_releases_resources(self):
        """Verify shutdown cancel failure still awaits loop and releases resources."""
        one=self.kilo.add(1);self.kilo.delay=.08;self.worker.set_enabled(True)
        await self.worker.start()
        while self.kilo.active is None:await asyncio.sleep(.005)
        handlers=tuple(self.worker.logger.handlers)
        with patch.object(self.kilo,'cancel',side_effect=OSError('PRIVATE FAILURE CONTENT')):
            await self.worker.shutdown()
        self.assertIsNone(self.kilo.active)
        self.assertEqual(self.kilo.calls,[one])
        self.assertIsNone(self.worker.lease)
        self.assertIsNone(self.worker.loop_task)
        self.assertEqual(self.worker.logger.handlers,[])
        self.assertTrue(all(handler._closed for handler in handlers))
        log=(self.root/'state/worker.log').read_text()
        self.assertIn('shutdown_cancel_failed',log)
        self.assertNotIn('PRIVATE FAILURE CONTENT',log)

    async def test_shutdown_loop_error_still_closes_handlers_and_releases_lease(self):
        """Verify shutdown loop error still closes handlers and releases lease."""
        self.own_without_loop()
        async def failed_loop():raise OSError('Fixture loop failure')
        self.worker.loop_task=asyncio.create_task(failed_loop())
        with self.assertRaises(OSError):await self.worker.shutdown()
        self.assertIsNone(self.worker.lease)
        self.assertIsNone(self.worker.loop_task)
        self.assertEqual(self.worker.logger.handlers,[])

    async def test_missing_stale_receipt_is_interrupted_without_disabling_recovery(self):
        """Verify missing stale receipt is interrupted without disabling recovery."""
        ident=self.kilo.add(1,'running',1)
        with self.db.connect() as c:
            c.execute('INSERT INTO continuity_receipts(job_id,task_id,owner,state,attempts,claimed_at) VALUES(?,?,?,?,?,?)',
                      (ident,1,'previous-owner','running',1,module.timestamp()))
        for status in (404,409):
            with self.db.connect() as c:c.execute("UPDATE continuity_receipts SET state='running' WHERE job_id=?",(ident,))
            with patch.object(self.kilo,'get',side_effect=HTTPException(status,'Fixture absent receipt')):
                self.assertTrue(self.worker.recover_owned())
            self.assertEqual(self.worker.status()['receipts'][0]['state'],'interrupted')
        with self.db.connect() as c:c.execute("UPDATE continuity_receipts SET state='running' WHERE job_id=?",(ident,))
        with patch.object(self.kilo,'get',side_effect=HTTPException(403,'Fixture denial')):
            with self.assertRaises(HTTPException):self.worker.recover_owned()

    async def test_stuck_adapter_has_bounded_shutdown_and_interrupted_receipt(self):
        """Verify stuck adapter has bounded shutdown and interrupted receipt."""
        ident=self.kilo.add(1);self.worker.set_enabled(True)
        async def stuck_start(job_id):
            self.kilo.active=job_id
            receipt=self.kilo.get(job_id);receipt.update(status='running',attempts=1);self.kilo.record(receipt)
            return receipt
        with patch.object(self.kilo,'start',side_effect=stuck_start), \
             patch.object(self.kilo,'cancel',side_effect=OSError('Fixture cancellation failure')), \
             patch.object(module,'SHUTDOWN_WAIT_SECONDS',.06):
            await self.worker.start()
            while self.worker.active is None:await asyncio.sleep(.005)
            await asyncio.wait_for(self.worker.shutdown(),timeout=1)
        self.assertIsNone(self.worker.loop_task);self.assertIsNone(self.worker.lease)
        self.assertEqual(self.worker.status()['receipts'][0]['state'],'interrupted')
        self.assertEqual(self.kilo.active,ident)  # No fabricated adapter completion or foreign lock release.
        self.kilo.active=None

    async def test_active_cycle_deadline_does_not_retry_or_claim_adapter_finished(self):
        """Verify active cycle deadline does not retry or claim adapter finished."""
        ident=self.kilo.add(1);self.own_without_loop();self.worker.set_enabled(True)
        async def stuck_start(job_id):
            self.kilo.active=job_id
            receipt=self.kilo.get(job_id);receipt.update(status='running',attempts=1);self.kilo.record(receipt)
            return receipt
        with patch.object(self.kilo,'start',side_effect=stuck_start),patch.object(module,'ACTIVE_WAIT_SECONDS',.01):
            self.assertEqual(await self.worker.cycle(),'adapter_completion_timeout')
        self.assertEqual(self.worker.status()['receipts'][0]['state'],'interrupted')
        self.assertEqual(await self.worker.cycle(),'waiting_for_existing_kilo')
        self.assertEqual(self.kilo.active,ident)
        self.kilo.active=None


class RouteTests(unittest.TestCase):
    def test_auth_origin_and_no_untrusted_payload_action(self):
        """Verify auth origin and no untrusted payload action."""
        with tempfile.TemporaryDirectory(dir='H:/NEXEN/temp') as temporary:
            root=Path(temporary);db=DB(root/'fixture.sqlite3');kilo=FakeKilo(db);mode=Mode();(root/'kilo').mkdir()
            original=module.ContinuityWorker
            def build(*args,**kwargs):return original(*args,state_dir=root/'state',kilo_root=root/'kilo',base=root,**kwargs)
            app=FastAPI()
            with patch.object(module,'ContinuityWorker',side_effect=build):module.register(app,db,kilo,mode)
            with TestClient(app,base_url='http://127.0.0.1:8788',client=('127.0.0.1',41000)) as client:
                self.assertEqual(client.get('/api/continuity/status',headers={'Host':'evil.invalid'}).status_code,403)
                self.assertEqual(client.post('/api/continuity/enable').status_code,403)
                self.assertEqual(client.post('/api/continuity/enable',headers={'Origin':'http://evil.invalid','X-Nexen-Action':'launch'}).status_code,403)
                response=client.post('/api/continuity/enable',headers={'Origin':'http://127.0.0.1:8788','X-Nexen-Action':'launch'})
                self.assertEqual(response.status_code,200);self.assertTrue(response.json()['enabled'])
                self.assertEqual(client.post('/api/continuity/run',json={'command':'anything'}).status_code,404)
            protected=FastAPI();auth=app_auth.AuthStore(root/'auth')
            with patch.object(app_auth,'AuthStore',return_value=auth):gate=app_auth.register(protected)
            @protected.middleware('http')
            async def guard(request,call_next):
                denied=gate(request);return denied if denied is not None else await call_next(request)
            with patch.object(module,'ContinuityWorker',side_effect=build):module.register(protected,db,kilo,mode)
            with TestClient(protected,base_url='http://127.0.0.1:8788',client=('127.0.0.1',41000)) as client:
                self.assertEqual(client.get('/api/continuity/status').status_code,401)
                self.assertEqual(client.post('/api/continuity/enable',headers={'Origin':'http://127.0.0.1:8788','X-Nexen-Action':'launch','X-Nexen-Service':auth.service_key}).status_code,401)


if __name__=='__main__':unittest.main()
