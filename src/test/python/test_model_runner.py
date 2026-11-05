import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from railsim_mcp.model_runner import model_identity, request_action, validate_action, check_runner
from railsim_mcp.runtime import bounded_process
from railsim_mcp.configuration import Settings

SCHEMA = dict(required=['action','volume','pattern','reason'], properties={
    'pattern':dict(items=dict(properties={'product':dict(enum=['FV','RV','GV'])}))})
ACTION = dict(action='submit',volume=4,pattern=[dict(product='FV',departure=0)],reason='test')


class ModelRunnerTest(unittest.TestCase):
    def test_explicit_copilot_model(self):
        with self.assertRaises(ValueError): model_identity('copilot',None)
        self.assertEqual(model_identity('codex',None)['model'],'gpt-6-luna')

    def test_credentials_not_recorded(self):
        with patch.dict(os.environ, {'COPILOT_PROVIDER_API_KEY':'secret','COPILOT_PROVIDER_BASE_URL':'http://localhost:8000/v1'}, clear=True):
            identity=model_identity('copilot','qwen-test')
        self.assertNotIn('secret',json.dumps(identity))
        with patch.dict(os.environ, {'COPILOT_PROVIDER_BASE_URL':'https://user:secret@example.com/v1'}):
            with self.assertRaises(ValueError): model_identity('copilot','qwen-test')

    def test_copilot_piped_json_and_isolation(self):
        def process(command,timeout,**kw):
            self.assertEqual(command[:2],['copilot','-s'])
            self.assertIn('--available-tools=',command)
            self.assertIn('--disable-builtin-mcps',command)
            self.assertEqual(kw['env']['COPILOT_AUTO_UPDATE'],'false')
            self.assertIn('JSON schema:',kw['input'])
            self.assertEqual(timeout,12)
            kw['stdout'].write(json.dumps(ACTION))
            return subprocess.CompletedProcess(command,0)
        with tempfile.TemporaryDirectory() as d:
            action,metrics=request_action(model_identity('copilot','test'), 'observation', SCHEMA, Path(d)/'1',12,process)
            self.assertEqual(action,ACTION)
            self.assertFalse(metrics['token_usage_available'])
            self.assertTrue((Path(d)/'1.metrics.json').exists())

    def test_codex_compatibility_and_usage(self):
        def process(command,timeout,**kw):
            Path(command[command.index('-o')+1]).write_text(json.dumps(ACTION))
            kw['stdout'].write(json.dumps(dict(type='turn.completed',usage={'input_tokens':12})))
            return subprocess.CompletedProcess(command,0)
        with tempfile.TemporaryDirectory() as d:
            action,metrics=request_action(model_identity('codex',None),'prompt',SCHEMA,Path(d)/'1',12,process)
        self.assertEqual(action,ACTION)
        self.assertEqual(metrics['usage'],[{'input_tokens':12}])

    def test_prose_and_execution_failure_are_not_repaired(self):
        for code,text in [(0,'Here is JSON: '+json.dumps(ACTION)),(1,'')]:
            def process(command,timeout,**kw):
                kw['stdout'].write(text)
                return subprocess.CompletedProcess(command,code)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises((ValueError,RuntimeError)):
                    request_action(model_identity('copilot','test'),'prompt',SCHEMA,Path(d)/'1',12,process)

    def test_action_contract(self):
        for change in [dict(volume=True),dict(action='shell'),dict(extra=1),dict(action='random'),
                       dict(pattern=[dict(product='FV',departure=True)]),dict(pattern=[dict(product='unknown',departure=1)])]:
            with self.subTest(change=change),self.assertRaises(ValueError): validate_action({**ACTION,**change},SCHEMA)



    def test_missing_cli_fails_preflight(self):
        with patch('shutil.which',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'not found'): check_runner(model_identity('copilot','test'),10,None)

    def test_real_subprocess_pipe_contract(self):
        # Exercise actual Popen/stdin/files rather than only a mocked function.
        import sys
        with tempfile.TemporaryDirectory() as d:
            executable=Path(d)/'copilot'
            executable.write_text('#!'+sys.executable+'\nimport json,sys\nassert "JSON schema:" in sys.stdin.read()\nprint('+repr(json.dumps(ACTION))+')\n')
            executable.chmod(0o755)
            with patch.dict(os.environ, {'PATH':d+os.pathsep+os.environ.get('PATH','')}):
                action,_=request_action(model_identity('copilot','fake'), 'test', SCHEMA, Path(d)/'1',10,bounded_process)
            self.assertEqual(action,ACTION)





class OpenAIRunnerTest(unittest.TestCase):
    """Direct OpenAI-compatible endpoint, exercised against a local fake vLLM server."""
    def serve(self, content, finish='stop', models=('nemotron',)):
        import http.server, threading
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                seen.append(('GET', self.path, dict(self.headers), None))
                self.reply(dict(data=[dict(id=m) for m in models]))
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(('POST', self.path, dict(self.headers), body))
                self.reply(dict(choices=[dict(message=dict(content=content), finish_reason=finish)], usage=dict(prompt_tokens=900, completion_tokens=20)))
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/v1', seen

    def environment(self, url, **extra):
        return patch.dict(os.environ, {'RAILSIM_OPENAI_BASE_URL': url, **extra}, clear=True)

    def test_schema_constrained_request_without_thinking(self):
        url, seen = self.serve(json.dumps(ACTION))
        extra = json.dumps({'chat_template_kwargs': {'enable_thinking': False}})
        with self.environment(url, RAILSIM_OPENAI_EXTRA_BODY=extra, RAILSIM_OPENAI_API_KEY='secret'), tempfile.TemporaryDirectory() as d:
            identity = model_identity('openai', 'nemotron')
            self.assertEqual(check_runner(identity, 5, bounded_process)['served_models'], ['nemotron'])
            action, metrics = request_action(identity, 'observation', SCHEMA, Path(d)/'1', 5, bounded_process)
            request = json.loads((Path(d)/'1.request.json').read_text())
        self.assertEqual(action, ACTION)
        self.assertEqual(metrics['usage'], [dict(prompt_tokens=900, completion_tokens=20)])
        method, path, headers, body = seen[-1]
        self.assertEqual((method, path), ('POST', '/v1/chat/completions'))
        self.assertEqual(body['response_format']['type'], 'json_schema')
        self.assertEqual(body['max_tokens'], 256)
        self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(headers['Authorization'], 'Bearer secret')
        self.assertNotIn('secret', json.dumps(identity) + json.dumps(request) + json.dumps(metrics))

    def test_prose_truncation_and_unserved_model_fail(self):
        for content, finish in [('Sure: ' + json.dumps(ACTION), 'stop'), (json.dumps(ACTION), 'length')]:
            url, _ = self.serve(content, finish)
            with self.environment(url), tempfile.TemporaryDirectory() as d, self.assertRaises((ValueError, RuntimeError)):
                request_action(model_identity('openai', 'nemotron'), 'p', SCHEMA, Path(d)/'1', 5, bounded_process)
        url, _ = self.serve('{}', models=('other',))
        with self.environment(url), self.assertRaisesRegex(RuntimeError, 'not served'):
            check_runner(model_identity('openai', 'nemotron'), 5, bounded_process)

    def test_endpoint_configuration_is_required_and_credential_free(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            model_identity('openai', 'nemotron')
        with self.environment('https://user:pw@example.com/v1'), self.assertRaises(ValueError):
            model_identity('openai', 'nemotron')


class AnthropicRunnerTest(unittest.TestCase):
    def test_haiku_without_thinking_uses_schema_output_and_rejects_truncation(self):
        from types import SimpleNamespace
        import anthropic
        sent = {}
        def response(stop):
            usage = SimpleNamespace(to_dict=lambda: {'input_tokens': 600, 'output_tokens': 20})
            return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type='text', text=json.dumps(ACTION))],
                                   usage=usage, to_json=lambda: '{}')
        for stop, ok in [('end_turn', True), ('max_tokens', False), ('refusal', False)]:
            client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (sent.update(kw), response(stop))[1]))
            with patch.object(anthropic, 'Anthropic', return_value=client), patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as d:
                identity = model_identity('anthropic', 'claude-haiku-5-5')
                if ok:
                    action, metrics = request_action(identity, 'p', SCHEMA, Path(d)/'1', 5, bounded_process)
                    self.assertEqual(action, ACTION)
                else:
                    with self.assertRaises(RuntimeError): request_action(identity, 'p', SCHEMA, Path(d)/'1', 5, bounded_process)
        self.assertEqual(sent['thinking'], {'type': 'disabled'})
        self.assertEqual(sent['output_config']['effort'], 'low')
        self.assertEqual(sent['output_config']['format']['type'], 'json_schema')
