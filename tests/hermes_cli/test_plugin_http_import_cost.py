"""Catalog discovery stays independent of optional HTTP operations."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("configured_auth", [False, True])
def test_cold_kanban_retains_catalog_and_committed_hook_without_http_clients(tmp_path, configured_auth):
    home = tmp_path / "home"
    hh = home / ".hermes"
    plugin = hh / "plugins" / "record-block"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(json.dumps({"name": "record-block"}))
    (plugin / "__init__.py").write_text("""
import json
import sqlite3
from hermes_constants import get_hermes_home
def register(ctx):
    def blocked(task_id, **kwargs):
        home = get_hermes_home()
        with sqlite3.connect(home / 'kanban.db') as conn:
            state = conn.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
        with (home / 'observed.jsonl').open('a') as out:
            out.write(json.dumps({'task_id': task_id, 'status': state}) + '\\n')
    ctx.register_hook('kanban_task_blocked', blocked)
""")
    config = {"plugins": {"enabled": ["record-block"]}}
    if configured_auth:
        config["dashboard"] = {"oauth": {
            "client_id": "agent:private-fixture", "portal_url": "https://fixture.invalid",
            "self_hosted": {"issuer": "https://fixture.invalid", "client_id": "private-fixture"},
        }}
    (hh / "config.yaml").write_text(json.dumps(config))
    env = os.environ | {"HOME": str(home), "HERMES_HOME": str(hh), "HERMES_MANAGED_DIR": str(home / "managed")}
    for key in list(env):
        if key.startswith("HERMES_DASHBOARD_") or key.endswith("API_KEY") or key in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(key)
    script = """
import json,sys
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect_closing() as conn:
    tid = kb.create_task(conn, title='private fixture')
from hermes_cli.main import main
sys.argv = ['hermes', 'kanban', 'block', tid, 'private fixture']
assert main() in (None, 0)
from agent import browser_registry, image_gen_registry, video_gen_registry, web_search_registry
for registry, names in [(browser_registry, ['browser-use','browserbase','firecrawl']),
                        (image_gen_registry, ['krea','xai']), (video_gen_registry, ['xai']),
                        (web_search_registry, ['tavily','firecrawl'])]:
    for name in names:
        assert registry.get_provider(name) is not None, name
from hermes_cli.dashboard_auth import get_provider
for name in ['nous','self-hosted']:
    assert (get_provider(name) is not None) == (sys.argv[-1] == 'configured')
assert 'requests' not in sys.modules and 'httpx' not in sys.modules, 'Catalog discovery loaded unused HTTP clients'
print(json.dumps({'id': tid}))
"""
    # main() rewrites argv above; retain the fixture choice as a separate value.
    script = script.replace("import json,sys", "import json,sys\nconfigured = sys.argv[1] == 'configured'").replace("(sys.argv[-1] == 'configured')", "configured")
    r = subprocess.run([sys.executable, "-c", script, "configured" if configured_auth else "unset"],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    tid = json.loads(r.stdout.splitlines()[-1])["id"]
    assert [json.loads(line) for line in (hh / "observed.jsonl").read_text().splitlines()] == [
        {"task_id": tid, "status": "blocked"}]


def test_cold_registered_providers_use_real_http_transport_on_first_request(tmp_path):
    env = os.environ | {"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / ".hermes"),
                        "HERMES_MANAGED_DIR": str(tmp_path / "managed")}
    for key in list(env):
        if key.startswith("HERMES_DASHBOARD_") or key.endswith("API_KEY") or key in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(key)
    script = """
import asyncio,json,logging,sys,threading,urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hermes_cli.plugins import discover_plugins
discover_plugins()
assert 'requests' not in sys.modules and 'httpx' not in sys.modules, 'Discovery eagerly loaded transport'
seen=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self): self.respond()
    def do_POST(self): self.respond()
    def do_PATCH(self): self.respond()
    def respond(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        seen.append((self.command,self.path,dict(self.headers),body))
        code=200
        if self.path.endswith('/.well-known/openid-configuration'):
            self.send_response(302);self.send_header('Location','/discovery');self.end_headers();return
        if self.path=='/discovery':
            payload={'issuer':origin,'authorization_endpoint':origin+'/authorize','token_endpoint':origin+'/token',
                     'jwks_uri':origin+'/jwks','revocation_endpoint':origin+'/revoke'}
        elif self.path=='/browsers': payload={'id':'browser-id','cdpUrl':'ws://fixture.invalid'}
        elif self.path=='/token': payload={'id_token':'fixture-id-token','token_type':'Bearer'}
        elif self.path=='/bad-token': code=400;payload={'error':'invalid_grant'}
        elif self.path=='/image-generation-models': payload={'models':[{'id':'fixture-model'}]}
        elif self.path=='/videos/generations': payload={'request_id':'video-id'}
        elif self.path=='/videos/video-id': payload={'status':'done','video':{'url':'https://fixture.invalid/video.mp4'}}
        elif self.path.startswith('/jobs/'): payload={'status':'completed','result':{'url':'https://fixture.invalid/image.png'}}
        elif self.path.startswith('/enhance'): payload={'job_id':'enhance-id'}
        else: payload={'result':'ok'}
        raw=json.dumps(payload).encode();self.send_response(code);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);origin='http://127.0.0.1:'+str(server.server_port)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
    from agent.browser_registry import get_provider
    browser=get_provider('browser-use');browser._get_config=lambda:{'base_url':origin,'api_key':'browser-fixture','managed_mode':False}
    result=browser.create_session('fixture');assert result['bb_session_id']=='browser-id'
    assert browser.close_session('browser-id')
    from plugins.dashboard_auth.self_hosted import SelfHostedOIDCProvider
    from hermes_cli.dashboard_auth import InvalidCodeError
    from plugins.dashboard_auth._shared import exchange_token
    oidc=SelfHostedOIDCProvider(issuer=origin,client_id='oidc-fixture',client_secret='private-secret')
    assert '/authorize?' in oidc.start_login(redirect_uri=origin+'/auth/callback').redirect_url
    token,payload=exchange_token(origin+'/token',{'code':'private-code'},bad_request_exc=InvalidCodeError,
        idp='fixture',endpoint='token',token_key='id_token',missing_msg='missing')
    assert token=='fixture-id-token'
    try:
        exchange_token(origin+'/bad-token',{},bad_request_exc=InvalidCodeError,idp='fixture',endpoint='token',token_key='id_token',missing_msg='missing')
    except InvalidCodeError: pass
    else: raise AssertionError('HTTP400 must remain invalid code')
    oidc.revoke_session(refresh_token='private-refresh')
    from plugins.web._common import http_get_json
    assert http_get_json('fixture',origin+'/search',params={'q':'hello'},headers={'X-Fixture':'web'},timeout=2,logger=logging.getLogger())[0]=={'result':'ok'}
    from plugins.web.firecrawl.provider import _KeylessFirecrawlClient
    assert _KeylessFirecrawlClient(origin).search(query='hello')['result']=='ok'
    from plugins.web.tavily import provider as tavily
    tavily.provider_env=lambda key: origin if key=='TAVILY_BASE_URL' else ''
    assert tavily._tavily_request('tavily',{'query':'hello'},api_key='web-fixture')['result']=='ok'
    from plugins.spotify.client import SpotifyClient
    spotify=SpotifyClient.__new__(SpotifyClient);spotify._runtime={'base_url':origin,'access_token':'spotify-fixture'}
    assert spotify.request('GET','/spotify')['result']=='ok'
    from plugins.image_gen import krea,xai
    krea._POLL_INITIAL_INTERVAL=0
    assert krea._poll_krea_job(origin,'krea-fixture','job')['status']=='completed'
    krea._enhance_image(origin,'krea-fixture','https://fixture.invalid/image.png','fixture',managed=False)
    xai.resolve_xai_http_credentials=lambda:{'base_url':origin,'api_key':'xai-fixture'}
    assert 'fixture-model' in xai._fetch_live_models()
    from plugins.video_gen import xai as video
    assert asyncio.run(video._submit_xai_video_payload('video-fixture',origin,'generations',
        {'prompt':'fixture','model':'fixture-model'},modality='text',operation='generation',aspect_ratio='landscape',duration=5,resolution=None))['success']
    calls={(method,path):(headers,body) for method,path,headers,body in seen}
    assert json.loads(calls['POST','/browsers'][1])=={}
    assert calls['POST','/browsers'][0]['X-Browser-Use-API-Key']=='browser-fixture'
    assert calls['PATCH','/browsers/browser-id'][0]['X-Browser-Use-API-Key']=='browser-fixture'
    assert calls['POST','/token'][0]['Accept']=='application/json'
    assert urllib.parse.parse_qs(calls['POST','/token'][1].decode())=={'code':['private-code']}
    assert calls['POST','/revoke'][0]['Authorization'].startswith('Basic ')
    assert urllib.parse.parse_qs(calls['POST','/revoke'][1].decode())['token']==['private-refresh']
    assert calls['POST','/tavily'][0]['Authorization']=='Bearer web-fixture'
    assert 'Authorization' not in calls['POST','/v2/search'][0]
    assert calls['GET','/spotify'][0]['Authorization']=='Bearer spotify-fixture'
    assert calls['POST','/videos/generations'][0]['Authorization']=='Bearer video-fixture'
finally:
    server.shutdown();server.server_close();thread.join(timeout=2)
"""
    r = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
