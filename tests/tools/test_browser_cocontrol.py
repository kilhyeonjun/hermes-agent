"""The opt-in local lane is authoritative for every registered browser action."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading

from tools.browser_extension_router import routed_browser_handler


def configure(path):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"browser": {"cocontrol": {
        "enabled": True, "socket": str(path), "task_id": "task", "session_id": "session",
    }}}))


def no_legacy():
    raise AssertionError("configured co-control lane fell back to a different browser")


def test_registered_lane_uses_private_socket_and_same_task():
    with tempfile.TemporaryDirectory(prefix="hc-", dir="/tmp") as directory:
        parent = Path(directory).resolve()
        path = parent / "a.sock"
        configure(path)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        path.chmod(0o600)
        server.listen()
        seen = []
        def receive():
            connection, _ = server.accept()
            with connection:
                request = json.loads(connection.makefile("rb").readline())
                seen.append(request)
                connection.sendall(b'{"ok":true}\n')
        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        try:
            result = routed_browser_handler("browser_click", {"ref": "fixture"}, fallback=no_legacy,
                                            task_id="task", session_id="session")
            assert json.loads(result)["ok"] is True
            thread.join(2)
            assert seen == [{"task_id": "task", "session_id": "session", "action": "browser_click",
                             "args": {"ref": "fixture"}}]
        finally:
            server.close()


def test_authoritative_lane_rejects_wrong_identity_missing_socket_and_privileged_actions():
    with tempfile.TemporaryDirectory(prefix="hc-", dir="/tmp") as directory:
        path = Path(directory).resolve() / "missing.sock"
        configure(path)
        for action in ("browser_snapshot", "browser_exec", "browser_dialog", "browser_cdp",
                       "browser_console", "browser_vision", "browser_get_images"):
            result = routed_browser_handler(action, {}, fallback=no_legacy, task_id="task", session_id="session")
            assert "error" in json.loads(result)
        result = routed_browser_handler("browser_click", {}, fallback=no_legacy,
                                        task_id="other-task", session_id="session")
        assert "error" in json.loads(result)


def test_opt_in_advertises_regular_browser_tools_without_other_backend(monkeypatch):
    configure(Path("/private/tmp/owned-cocontrol/agent.sock"))
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "config.yaml"
    config = json.loads(path.read_text())
    config["browser"]["backend"] = "browser-use"
    path.write_text(json.dumps(config))
    from tools.browser_use_cli import is_browser_use_cli_mode
    from tools import browser_tool_install
    def missing(**kwargs):
        raise FileNotFoundError("synthetic missing legacy CLI")
    monkeypatch.setattr(browser_tool_install, "_find_agent_browser", missing)
    assert not is_browser_use_cli_mode()
    assert browser_tool_install.check_browser_requirements()


def test_engaged_gateway_context_cannot_be_overridden_by_explicit_session():
    from gateway.session_context import set_session_vars,clear_session_vars,reset_session_vars
    from unittest import mock
    with tempfile.TemporaryDirectory(prefix='hc-context-',dir='/private/tmp') as temporary:
        path=Path(temporary)/'agent.sock'
        server=socket.socket(socket.AF_UNIX);server.bind(str(path));path.chmod(0o600)
        configure(path)
        tokens=set_session_vars(session_id='actual-context')
        try:
            with mock.patch('tools.browser_cocontrol.socket.socket',side_effect=AssertionError('foreign socket opened')):
                result=routed_browser_handler('browser_snapshot',{},fallback=no_legacy,task_id='task',session_id='session')
                assert 'error' in json.loads(result)
                clear_session_vars(tokens)
                result=routed_browser_handler('browser_snapshot',{},fallback=no_legacy,task_id='task',session_id='session')
                assert 'error' in json.loads(result)
        finally:reset_session_vars();server.close()


def test_parallel_gateway_contexts_select_separate_signed_private_bindings():
    import asyncio
    from gateway.session_context import set_session_vars,reset_session_vars
    from hermes_constants import get_hermes_home
    with tempfile.TemporaryDirectory(prefix='hc-multi-',dir='/private/tmp') as temporary:
        parent=Path(temporary);records={};servers=[];threads=[];seen=[]
        for name in ('alpha','beta'):
            state=parent/name;state.mkdir(mode=0o700)
            path=state/'a.sock';server=socket.socket(socket.AF_UNIX);server.bind(str(path));path.chmod(0o600);server.listen();servers.append(server)
            marker=state/'browser-harness-session.json'
            marker.write_text(json.dumps({'active':True,'session_id':name,'task_id':name,'owner':name,
                'claim_nonce':name,'agent_key':'a'*64,'agent_socket':str(path)}));marker.chmod(0o600)
            records[name]={'task_id':name,'session_id':name,'socket':str(path),'runtime':str(state)}
            def receive(server=server,name=name):
                connection,_=server.accept()
                with connection:
                    body=json.loads(connection.makefile('rb').readline());seen.append((name,body))
                    connection.sendall(b'{"ok":true}\n')
            thread=threading.Thread(target=receive,daemon=True);thread.start();threads.append(thread)
        home=get_hermes_home();home.mkdir(parents=True,exist_ok=True)
        (home/'config.yaml').write_text(json.dumps({'browser':{'cocontrol':{'enabled':True,'bindings':records}}}))
        async def call(name):
            set_session_vars(session_id=name)
            try:
                await asyncio.sleep(0)
                return routed_browser_handler('browser_snapshot',{},fallback=no_legacy,session_id=name)
            finally:reset_session_vars()
        async def run():return await asyncio.gather(call('alpha'),call('beta'))
        try:
            assert all(json.loads(value).get('ok') for value in asyncio.run(run()))
            for thread in threads:thread.join(2)
            assert {name for name,_ in seen}=={'alpha','beta'}
            for name,body in seen:
                assert body['session_id']==name and body['claim_nonce']==name and body['signature']
        finally:
            for server in servers:server.close()


def test_new_bindings_require_actual_bound_context_even_before_global_latch(monkeypatch):
    import gateway.session_context as context
    from unittest import mock
    from hermes_constants import get_hermes_home
    with tempfile.TemporaryDirectory(prefix='hc-bound-',dir='/private/tmp') as temporary:
        state=Path(temporary);path=state/'a.sock'
        server=socket.socket(socket.AF_UNIX);server.bind(str(path));path.chmod(0o600)
        marker=state/'browser-harness-session.json'
        marker.write_text(json.dumps({'active':True,'session_id':'alpha','task_id':'alpha','owner':'alpha',
            'claim_nonce':'alpha','agent_key':'a'*64,'agent_socket':str(path)}));marker.chmod(0o600)
        binding={'task_id':'alpha','session_id':'alpha','socket':str(path),'runtime':str(state)}
        home=get_hermes_home();home.mkdir(parents=True,exist_ok=True)
        (home/'config.yaml').write_text(json.dumps({'browser':{'cocontrol':{'enabled':True,'bindings':{'alpha':binding}}}}))
        context.reset_session_vars()
        monkeypatch.setattr(context,'_session_context_engaged',False)
        try:
            with mock.patch('tools.browser_cocontrol.socket.socket',side_effect=AssertionError('unbound socket opened')):
                assert 'error' in json.loads(routed_browser_handler('browser_snapshot',{},fallback=no_legacy,session_id='alpha'))
                monkeypatch.setenv('HERMES_SESSION_ID','alpha')
                assert 'error' in json.loads(routed_browser_handler('browser_snapshot',{},fallback=no_legacy))
                with context.scoped_current_session_id('alpha'):
                    assert 'error' in json.loads(routed_browser_handler('browser_snapshot',{},fallback=no_legacy,session_id='beta'))
        finally:context.reset_session_vars();server.close()
