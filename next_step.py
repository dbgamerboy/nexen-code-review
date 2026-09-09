"""One selected NEXEN task, explicit user completion and code-owned destinations.

This module never runs task text, submits provider work or marks other tasks done.
The video on /next is a browser-local captioned plan, not a recorded app session.
"""
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from task_tracking import Tracker, TaskUpdate, now

BASE = Path(__file__).resolve().parent

# Only these developer-owned mappings can resolve a task to a workspace. Neither
# its imported title nor next_step may introduce a URL, shell command or adapter.
WORKSPACES = {
    'daily': '/day', 'discord': '/', 'track-all': '/tasks',
    'completion-memory': '/tasks', 'gui-blockers': '/connections',
    'photo-vision-jarvis': '/problems', 'photos-life': '/problems',
    'photo-memories': '/problems', 'local-lab': '/lab',
    'music-catalog': '/lab', 'music-releases': '/lab', 'music-social': '/plans',
    'music-batch-stems': '/music-render',
    'wdr-world': '/game', 'wdr-avatar': '/game', 'wdr-cars': '/game',
    'wdr-factory': '/game', 'wdr-music': '/game', 'wdr-store': '/lookbook',
    'wdr-tv': '/game', 'shared-game-pc-actions': '/game',
    'memory-all': '/plans', 'source-conflicts': '/plans', 'f-scan': '/plans',
    'named-folders-ideas': '/plans', 'archives': '/plans',
    'education': '/guide', 'hermes-openclaw-guide': '/guide',
    'automation-ranking': '/money', 'store-supplier': '/money',
    'ad-cap': '/money', 'backlog-audit': '/plans',
    'handoff': '/api/hub/document/handoff', 'learning-links': '/plans',
}
PROVIDERS = {
    'n8n': ('n8n',), 'n8n-flows': ('n8n',), 'pc2': ('pc2',),
    'store-payment': ('amboras',), 'store-domain': ('amboras',),
    'store-v2': ('amboras',), 'ad-account': ('ads',),
    'ad-video': ('supercool',), 'walkthrough': ('supercool',),
    'ad-launch': ('amboras', 'ads'), 'mobile-private': ('phone',),
    'phone-encrypted': ('phone',),
}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['click', 'voice'] = 'click'


class CompletionBody(ActionBody):
    outcome: str = Field(default='', max_length=4000)


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: int = Field(gt=0, strict=True)


class NextSteps:
    def __init__(self, db, requirements=None, seeds=None):
        self.db, self.tracker, self.requirements = db, Tracker(db), requirements
        self.seeds = {}
        path = Path(seeds) if seeds is not None else BASE/'data'/'task-seeds.json'
        try:
            if path.stat().st_size <= 2*1024*1024:
                payload = json.loads(path.read_text(encoding='utf-8-sig'))
                tasks = payload.get('tasks', []) if isinstance(payload, dict) else []
                tasks = tasks if isinstance(tasks, list) else []
                self.seeds = {x['key']: x for x in tasks
                              if isinstance(x, dict) and isinstance(x.get('key'), str)}
        except (OSError, ValueError, TypeError):
            pass
        with db.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS next_selection(
              slot INTEGER PRIMARY KEY CHECK(slot=1),task_id INTEGER NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS next_completion_claims(
              task_id INTEGER NOT NULL,baseline_history INTEGER NOT NULL,
              state TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(task_id,baseline_history));
            ''')

    def seed_key(self, task_id):
        return self.db.scalar('SELECT seed_key FROM task_details WHERE request_id=?', (task_id,)) or ''

    def task(self, task_id):
        item = self.tracker.get(task_id)
        key = self.seed_key(task_id)
        seed = self.seeds.get(key, {})
        urls = seed.get('source_urls', [])
        item['provenance'] = {
            'basis': 'task_seed' if seed else 'saved_task_and_history',
            'source': str(seed.get('source') or 'Saved local task; review its recorded history.'),
            'source_urls': [u for u in urls[:10] if isinstance(u, str)
                            and urlsplit(u).scheme == 'https'] if isinstance(urls, list) else [],
            'checked_date': seed.get('checked_date'), 'seed_key': key or None,
            'next_step_basis': 'Current saved task instruction; provider execution and results are checked separately.',
        }
        return item

    def select(self, task_id):
        self.tracker.get(task_id)
        with self.db.connect() as c:
            c.execute('''INSERT INTO next_selection VALUES(1,?,?) ON CONFLICT(slot)
                         DO UPDATE SET task_id=excluded.task_id,updated_at=excluded.updated_at''', (task_id, now()))
        return self.packet(task_id)

    def packet(self, task_id=None):
        # Housing is first in the choices and is the initial choice. A deliberate
        # selection stays selected even after completion until the user changes it.
        listing = self.tracker.list(include_done=False, limit=500)
        rows = listing['tasks']
        rent = self.db.scalar("SELECT r.id FROM task_details d JOIN hub_requests r ON r.id=d.request_id WHERE d.seed_key='rent' AND r.status!='done'")
        if rent is not None:
            if not any(t['id'] == rent for t in rows):
                rows.insert(0, self.tracker.get(rent))
            rows.sort(key=lambda t: t['id'] != rent)
        persisted = self.db.scalar('SELECT task_id FROM next_selection WHERE slot=1')
        chosen = task_id if task_id is not None else persisted
        if chosen is None:
            chosen = rows[0]['id'] if rows else None
        selected = None
        if chosen is not None:
            try:
                selected = self.task(chosen)
            except HTTPException:
                if task_id is not None:
                    raise
                selected = self.task(rows[0]['id']) if rows else None
        if selected and not any(t['id'] == selected['id'] for t in rows):
            rows.append({k: v for k, v in selected.items() if k not in ('history', 'provenance')})
        return dict(selected_task=selected, tasks=rows, total_open=listing['total'],
                    choices_truncated=listing['total'] > 500,
                    selection_persisted=bool(selected and selected['id'] == persisted),
                    action=self.action(selected) if selected else None,
                    coverage='Up to 500 open tasks, the housing priority and your selected task. See Tasks for the complete history.',
                    executed=False)

    def action(self, task):
        if task is None:
            raise HTTPException(404, 'Select an existing task first')
        base = dict(task_id=task['id'], executed=False, blockers=[], url=None)
        if task['status'] == 'done':
            return dict(base, status='completed', message='COMPLETED was recorded from your report. Reopen this same task in Task room if more work is needed.', url='/tasks')
        key = self.seed_key(task['id'])
        if key == 'rent' or key.startswith('rent-'):
            return dict(base, status='needs_human', url='/api/hub/document/rent',
                        message='Open the verified assistance guide, review the notice and make the actual contact. Record the outcome on this task; no call or payment is implied.')
        dependencies = PROVIDERS.get(key, ())
        if dependencies:
            try:
                provider = self.requirements() if callable(self.requirements) else self.requirements
                status = provider.status() if provider else {}
                observed = {i['id']: i for i in status.get('pending', []) if isinstance(i, dict) and 'id' in i}
            except Exception:
                observed = {}
            blockers = []
            for name in dependencies:
                record = observed.get(name)
                if not record or not (record.get('verified') is True and record.get('executable') is True):
                    blockers.append(dict(provider=name, state=record.get('state', 'unverified') if record else 'unverified',
                                         message=record.get('action', 'Verify the connection and executor in Connections.') if record else 'Connection status is unavailable. Verify it before proceeding.',
                                         evidence=record.get('evidence', '') if record else 'No verified readiness observation is available.'))
            if blockers:
                return dict(base, status='blocked', url='/connections', blockers=blockers,
                            message='This provider action is blocked. Finish the listed setup or login steps and verify the connection.')
            return dict(base, status='needs_adapter', url='/connections',
                        message='Connections report ready, but this page has no verified executor for that action. Nothing was submitted.')
        if task['status'] == 'blocked':
            return dict(base, status='blocked', url='/tasks',
                        message='This task is marked blocked. Review its next step and record what resolves the blocker before continuing.')
        if key in WORKSPACES:
            return dict(base, status='workspace_ready', url=WORKSPACES[key],
                        message='Open the connected NEXEN workspace to take the next step. Opening it does not complete this task or run a workflow.')
        return dict(base, status='needs_adapter', url='/tasks',
                    message='No verified task-specific executor is attached. Review the task in Task room or use Photo problem to clarify the next step.')

    def complete(self, task_id, body):
        # A durable claim excludes simultaneous/replayed completion requests. Its
        # history revision permits a fresh completion after an explicit reopen.
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT status FROM hub_requests WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HTTPException(404, 'Task not found')
            c.execute('''INSERT INTO next_selection VALUES(1,?,?) ON CONFLICT(slot)
                         DO UPDATE SET task_id=excluded.task_id,updated_at=excluded.updated_at''', (task_id, now()))
            revision = c.execute('SELECT coalesce(max(id),0) FROM task_history WHERE request_id=?', (task_id,)).fetchone()[0]
            already_done = row[0] == 'done'
            claimed = False
            if not already_done:
                claimed = c.execute("INSERT OR IGNORE INTO next_completion_claims VALUES(?,?,'started',?,?)",
                                    (task_id, revision, body.source, now())).rowcount == 1
        if already_done or not claimed:
            task = self.task(task_id)
            return dict(task=task, changed=False, celebrate=False, executed=False,
                        completion_basis='user_reported', action=self.action(task),
                        status='already_completed' if task['status'] == 'done' else 'completion_pending',
                        message='This task is already COMPLETED.' if task['status'] == 'done' else
                        'A completion request is already in progress or awaiting reconciliation. Refresh this task; no duplicate completion was submitted.')
        outcome = 'User reported via '+body.source+': '+(body.outcome.strip() or 'Marked this selected task COMPLETED.')
        try:
            self.tracker.update(task_id, TaskUpdate(status='done', outcome=outcome[:4000]))
        except Exception:
            # If history committed but its vault mirror failed, the completion is
            # still real. Otherwise permit an explicit later retry, never auto-run.
            if self.tracker.get(task_id)['status'] != 'done':
                with self.db.connect() as c:
                    c.execute('DELETE FROM next_completion_claims WHERE task_id=? AND baseline_history=?', (task_id, revision))
                raise
        with self.db.connect() as c:
            c.execute("UPDATE next_completion_claims SET state='completed' WHERE task_id=? AND baseline_history=?", (task_id, revision))
        task = self.task(task_id)
        return dict(task=task, changed=True, celebrate=True, executed=False,
                    completion_basis='user_reported', action=self.action(task), status='completed',
                    message='COMPLETED saved to this task and its local completion history. This records your report, not independent verification of an external result.')


def register(app, db):
    from pc_control import validate_request
    flow = NextSteps(db, requirements=lambda: getattr(app.state, 'requirements', None))

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    @app.get('/next', response_class=HTMLResponse)
    def page(request: Request):
        validate_request(request)
        return HTMLResponse((BASE/'next.html').read_text(encoding='utf-8'), headers={'Cache-Control': 'no-store'})

    @app.get('/api/next')
    def listing(request: Request, task_id: int | None = None):
        validate_request(request)
        return response(flow.packet(task_id))

    @app.post('/api/next/select')
    def select(body: SelectionBody, request: Request):
        validate_request(request, mutation=True)
        return response(flow.select(body.task_id))

    @app.post('/api/next/{task_id}/complete')
    def complete(task_id: int, body: CompletionBody, request: Request):
        validate_request(request, mutation=True)
        return response(flow.complete(task_id, body))

    @app.post('/api/next/{task_id}/do')
    def do(task_id: int, body: ActionBody, request: Request):
        validate_request(request, mutation=True)
        return response(flow.action(flow.task(task_id)))

    return flow
