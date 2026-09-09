"""Single-owner password sessions. Passwords and session tokens are never stored raw."""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent/'data/auth'
COOKIE = 'nexen_session'
ITERATIONS = 600_000
SESSION_SECONDS = 12*60*60

class PasswordBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=12, max_length=256)

class AuthStore:
    def __init__(self, root=ROOT):
        self.root=Path(root);self.root.mkdir(parents=True, exist_ok=True)
        self.path=self.root/'sessions.sqlite3'
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS owner(id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT,hash TEXT,iterations INTEGER);
              CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,created REAL,expires REAL);
              CREATE TABLE IF NOT EXISTS login_attempts(at REAL,success INTEGER);''')
        self.key_path=self.root/'service.key'
        if not self.key_path.exists():
            try:
                with self.key_path.open('x',encoding='ascii') as out:out.write(secrets.token_urlsafe(48))
            except FileExistsError:pass
        self.service_key=self.key_path.read_text(encoding='ascii').strip()

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def configured(self):
        with self.connect() as c:return c.execute('SELECT 1 FROM owner').fetchone() is not None

    @staticmethod
    def password_hash(password,salt,iterations=ITERATIONS):
        return hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),bytes.fromhex(salt),iterations).hex()

    def new_session(self,c):
        token=secrets.token_urlsafe(32);now=time.time()
        c.execute('DELETE FROM sessions WHERE expires<?',(now,))
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),now,now+SESSION_SECONDS))
        return token

    def setup(self,password):
        salt=secrets.token_hex(32);hashed=self.password_hash(password,salt)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM owner').fetchone():raise HTTPException(409,'A password is already set. Unlock the existing account.')
            c.execute('INSERT INTO owner VALUES(1,?,?,?)',(salt,hashed,ITERATIONS))
            return self.new_session(c)

    def login(self,password):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            now=time.time();c.execute('DELETE FROM login_attempts WHERE at<?',(now-86400,))
            failures=c.execute('SELECT count(*) FROM login_attempts WHERE at>? AND success=0',(now-900,)).fetchone()[0]
            if failures>=5:raise HTTPException(429,'Too many attempts. Wait 15 minutes before trying again.')
            owner=c.execute('SELECT * FROM owner').fetchone()
            valid=bool(owner and hmac.compare_digest(self.password_hash(password,owner['salt'],owner['iterations']),owner['hash']))
            c.execute('INSERT INTO login_attempts VALUES(?,?)',(now,int(valid)))
            if valid:
                c.execute('DELETE FROM login_attempts')
                token=self.new_session(c)
            else:token=None
        if token is None:raise HTTPException(401,'The password did not match.')
        return token

    def valid(self,token):
        if not token or len(token)>128:return False
        with self.connect() as c:return c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',
            (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone() is not None

    def logout(self,token):
        if token:
            with self.connect() as c:c.execute('DELETE FROM sessions WHERE hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

def register(app):
    store=AuthStore()

    def session_response(token,request):
        response=JSONResponse({'authenticated':True})
        response.set_cookie(COOKIE,token,max_age=SESSION_SECONDS,httponly=True,samesite='strict',
                            secure=bool(getattr(request.state,'private_access',False)),path='/')
        response.headers['Cache-Control']='no-store'
        return response

    @app.get('/login',response_class=HTMLResponse)
    def login_page():
        return (Path(__file__).parent/'login.html').read_text(encoding='utf-8')

    @app.get('/api/auth/session')
    def session(request:Request):
        return {'configured':store.configured(),'authenticated':store.valid(request.cookies.get(COOKIE)),
                'can_setup':not getattr(request.state,'private_access',False),
                'transport':'private_https' if getattr(request.state,'private_access',False) else 'local_loopback',
                'disk_encryption':'not_configured_by_nexen'}

    @app.post('/api/auth/setup')
    def setup(body:PasswordBody,request:Request):
        if getattr(request.state,'private_access',False):raise HTTPException(403,'Create the first password on the NEXEN PC.')
        return session_response(store.setup(body.password),request)

    @app.post('/api/auth/login')
    def login(body:PasswordBody,request:Request):return session_response(store.login(body.password),request)

    @app.post('/api/auth/logout')
    def logout(request:Request):
        store.logout(request.cookies.get(COOKIE));response=JSONResponse({'authenticated':False})
        response.delete_cookie(COOKIE,path='/');return response

    @app.get('/healthz')
    def health():return {'status':'ok'}

    def gate(request):
        path=request.url.path
        allowed={'/login','/api/auth/session','/api/auth/setup','/api/auth/login','/api/auth/logout'}
        if path in allowed or path in ('/branding/NEXEN-logo.png','/branding/NEXEN.ico'):return None
        private=getattr(request.state,'private_access',False)
        if path=='/healthz' and not private:return None
        # A fixed loopback-only service credential keeps the watchdog functioning.
        # It cannot unlock arbitrary routes or be used through Tailscale.
        keys=request.headers.getlist('x-nexen-service')
        service_paths={'/api/hub/digest','/api/memory/context','/api/lab/chat','/api/hub/request'}
        if not private and path in service_paths and len(keys)==1 and hmac.compare_digest(keys[0],store.service_key):return None
        if store.valid(request.cookies.get(COOKIE)):return None
        if path.startswith('/api/') or request.method!='GET':return JSONResponse({'detail':'Unlock NEXEN first.','login':'/login'},status_code=401)
        next_path=path if path in {'/game','/tasks','/day','/photos','/problems','/plans','/lab','/guide','/money','/lookbook','/connections',
                                  '/voice','/next','/desktop','/storage','/memory-pools','/automatic-mode','/problem-cases','/check-in',
                                  '/agentic-os','/youtube-memory','/code-review'} else '/'
        return RedirectResponse('/login?next='+next_path,status_code=303)

    return gate
