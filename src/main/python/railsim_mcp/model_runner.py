"""Planners for the bounded controller; never grant simulator tools to a model.

Runners: Codex CLI, Copilot CLI, a direct OpenAI-compatible HTTP endpoint
(vLLM, NIM, OpenRouter) for small self-hosted models in fast loops, or the
Anthropic SDK (e.g. Claude Haiku 5.5 with thinking disabled).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

RUNNERS = ('codex', 'copilot', 'openai', 'anthropic')
# Direct endpoint routing. The key is read from RAILSIM_OPENAI_API_KEY only and never recorded.
OPENAI_SETTINGS = ('RAILSIM_OPENAI_BASE_URL', 'RAILSIM_OPENAI_MAX_TOKENS', 'RAILSIM_OPENAI_EXTRA_BODY')


def plain_url(url):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('provider URL must not contain credentials, query or fragment')
    return url


def model_identity(runner, model):
    if runner not in RUNNERS:
        raise ValueError('unknown model runner')
    if runner in ('copilot', 'openai', 'anthropic') and not model:
        raise ValueError(runner+' requires --model; choose an ID from /model or your endpoint')
    if model is not None and (not model.strip() or model.startswith('-')):
        raise ValueError('invalid model ID')
    identity = dict(runner=runner, model=model or 'gpt-6-luna')
    if runner == 'codex' and os.environ.get('RAILSIM_CODEX_EFFORT'):
        identity['reasoning_effort'] = os.environ['RAILSIM_CODEX_EFFORT']
    if runner == 'copilot':
        # Record routing, never authentication. Reject credentials in the URL.
        url = plain_url(os.environ.get('COPILOT_PROVIDER_BASE_URL', ''))
        identity['provider'] = {key: os.environ.get(key, '') for key in (
            'COPILOT_PROVIDER_TYPE', 'COPILOT_PROVIDER_WIRE_API',
            'COPILOT_PROVIDER_MODEL_ID', 'COPILOT_PROVIDER_WIRE_MODEL',
            'COPILOT_PROVIDER_MAX_PROMPT_TOKENS', 'COPILOT_PROVIDER_MAX_OUTPUT_TOKENS')}
        identity['provider']['COPILOT_PROVIDER_BASE_URL'] = url
    if runner == 'openai':
        settings = {key: os.environ.get(key, '') for key in OPENAI_SETTINGS}
        if not settings['RAILSIM_OPENAI_BASE_URL']:
            raise ValueError('openai runner requires RAILSIM_OPENAI_BASE_URL, e.g. http://gpu-server:8000/v1')
        plain_url(settings['RAILSIM_OPENAI_BASE_URL'])
        settings['RAILSIM_OPENAI_MAX_TOKENS'] = int(settings['RAILSIM_OPENAI_MAX_TOKENS'] or 256)
        settings['RAILSIM_OPENAI_EXTRA_BODY'] = json.loads(settings['RAILSIM_OPENAI_EXTRA_BODY'] or '{}')
        if not isinstance(settings['RAILSIM_OPENAI_EXTRA_BODY'], dict):
            raise ValueError('RAILSIM_OPENAI_EXTRA_BODY must be a JSON object')
        identity['provider'] = settings
    if runner == 'anthropic':
        # Key from ANTHROPIC_API_KEY via the SDK; never recorded.
        identity['provider'] = dict(thinking=os.environ.get('RAILSIM_ANTHROPIC_THINKING', 'disabled'),
                                    effort=os.environ.get('RAILSIM_ANTHROPIC_EFFORT', 'low'),
                                    max_tokens=int(os.environ.get('RAILSIM_ANTHROPIC_MAX_TOKENS', 1024)))
        if os.environ.get('RAILSIM_ANTHROPIC_THINKING_DISPLAY'):
            identity['provider']['thinking_display'] = os.environ['RAILSIM_ANTHROPIC_THINKING_DISPLAY']
    return identity


def anthropic_action(identity, prompt, schema, prefix, timeout):
    """Messages API call with a JSON-schema output format; refusals and truncation are errors."""
    import anthropic
    settings = identity['provider']
    thinking = dict(type=settings['thinking'])
    if settings.get('thinking_display'):
        thinking['display'] = settings['thinking_display']
    request = dict(model=identity['model'], max_tokens=settings['max_tokens'], thinking=thinking,
                   output_config=dict(effort=settings['effort'], format=dict(type='json_schema', schema=schema)),
                   messages=[dict(role='user', content=prompt)])
    prefix.with_suffix('.request.json').write_text(json.dumps(request, indent=2))
    client = anthropic.Anthropic(timeout=max(1.0, timeout), max_retries=2)
    if settings['max_tokens'] > 16000:  # long thinking turns: stream so the HTTP request does not time out
        with client.messages.stream(**request) as stream:
            response = stream.get_final_message()
    else:
        response = client.messages.create(**request)
    prefix.with_suffix('.jsonl').write_text(response.to_json()+'\n')
    if response.stop_reason != 'end_turn':
        raise RuntimeError(f'model stopped with {response.stop_reason}; see {prefix.with_suffix(".jsonl")}')
    text = next((b.text for b in response.content if b.type == 'text'), '')
    return text, [response.usage.to_dict()]


def openai_post(identity, path, body, timeout):
    """One HTTP request; returns (status, parsed JSON). Errors are raised, never repaired."""
    url = identity['provider']['RAILSIM_OPENAI_BASE_URL'].rstrip('/') + path
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('RAILSIM_OPENAI_API_KEY')
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers=headers, method='GET' if body is None else 'POST')
    try:
        with urllib.request.urlopen(request, timeout=max(.01, timeout)) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'{url} returned HTTP {exc.code}: {exc.read()[:2000].decode(errors="replace")}') from None


def validate_action(action, schema):
    """Same strict contract for runners with and without native JSON schemas."""
    if not isinstance(action, dict) or set(action) != set(schema['required']):
        raise ValueError('model must return exactly action, volume, pattern, reason')
    if action['action'] not in ('random', 'rules', 'submit', 'stop'):
        raise ValueError('invalid model action')
    if type(action['volume']) is not int or not isinstance(action['reason'], str):
        raise ValueError('invalid model volume/reason')
    pattern = action['pattern']
    if not isinstance(pattern, list) or (action['action'] != 'submit' and pattern):
        raise ValueError('non-submit actions require an empty pattern')
    products = schema['properties']['pattern']['items']['properties']['product']['enum']
    for row in pattern:
        if (not isinstance(row, dict) or set(row) != {'product', 'departure'}
                or row['product'] not in products or type(row['departure']) is not int):
            raise ValueError('invalid model pattern row')
    return action


def openai_action(identity, prompt, schema, prefix, timeout):
    """Schema-constrained chat completion; vLLM enforces response_format json_schema."""
    settings = identity['provider']
    body = dict(model=identity['model'], messages=[dict(role='user', content=prompt)],
                max_tokens=settings['RAILSIM_OPENAI_MAX_TOKENS'], temperature=0,
                response_format=dict(type='json_schema', json_schema=dict(name='action', schema=schema, strict=True)))
    body.update(settings['RAILSIM_OPENAI_EXTRA_BODY'])
    prefix.with_suffix('.request.json').write_text(json.dumps(body, indent=2))
    reply = openai_post(identity, '/chat/completions', body, timeout)
    prefix.with_suffix('.jsonl').write_text(json.dumps(reply)+'\n')
    choice = reply['choices'][0]
    if choice.get('finish_reason') not in ('stop', None):
        raise RuntimeError('model output ended with finish_reason='+str(choice.get('finish_reason'))+'; raise RAILSIM_OPENAI_MAX_TOKENS or disable thinking')
    return choice['message']['content'] or '', [reply['usage']] if reply.get('usage') else []


def request_action(identity, prompt, schema, prefix, timeout, process, validate=None):
    validate = validate or validate_action
    prefix = Path(prefix).resolve()
    schema_path = prefix.parent/'schema.json'
    schema_path.write_text(json.dumps(schema))
    action_path = prefix.with_suffix('.action.json')
    env = os.environ.copy()
    started = time.monotonic()
    if identity['runner'] in ('openai', 'anthropic'):
        call = openai_action if identity['runner'] == 'openai' else anthropic_action
        raw, usage = call(identity, prompt, schema, prefix, timeout)
        action = json.loads(raw)  # Prose or markdown fails here; nothing is salvaged.
        validate(action, schema)
        action_path.write_text(json.dumps(action, indent=2)+'\n')
        metrics = dict(**identity, elapsed_seconds=time.monotonic()-started,
                       usage=usage, token_usage_available=bool(usage), output_bytes=len(raw.encode()))
        prefix.with_suffix('.metrics.json').write_text(json.dumps(metrics, indent=2)+'\n')
        return action, metrics
    if identity['runner'] == 'codex':
        command = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                   '--sandbox', 'read-only', '-c', 'features.shell_tool=false', '--model', identity['model'],
                   '--json', '--output-schema', str(schema_path), '-o', str(action_path), '-']
        if identity.get('reasoning_effort'):
            command[command.index('--model'):command.index('--model')] = [
                '-c', 'model_reasoning_effort=' + identity['reasoning_effort']]
    else:
        # No tools exposed: Copilot is a planner, Python alone executes/validates.
        # A fresh CLI session per action receives the same bounded observation.
        command = ['copilot', '-s', '--model', identity['model'], '--available-tools=',
                   '--disable-builtin-mcps', '--no-custom-instructions', '--no-ask-user']
        env['COPILOT_AUTO_UPDATE'] = 'false'
        prompt += '\nReturn ONLY a JSON object, without markdown or commentary. JSON schema:\n' + json.dumps(schema)
    with prefix.with_suffix('.jsonl').open('w') as out, prefix.with_suffix('.stderr').open('w') as err:
        result = process(command, timeout, cwd=prefix.parent, stdin=subprocess.PIPE,
                         stdout=out, stderr=err, text=True, input=prompt, env=env)
    if result.returncode:
        raise RuntimeError(identity['runner'] + ' execution failed; see ' + str(prefix.with_suffix('.stderr')))
    raw = prefix.with_suffix('.jsonl').read_text()
    usage = []
    if identity['runner'] == 'codex':
        for line in raw.splitlines():
            event = json.loads(line)
            if event.get('type') == 'turn.completed':
                usage.append(event.get('usage'))
        action = json.loads(action_path.read_text())
    else:
        # Do not salvage JSON from prose or silently substitute a policy.
        action = json.loads(raw)
    validate(action, schema)
    action_path.write_text(json.dumps(action, indent=2)+'\n')
    metrics = dict(**identity, elapsed_seconds=time.monotonic()-started,
                   usage=usage, token_usage_available=bool(usage), output_bytes=len(raw.encode()))
    prefix.with_suffix('.metrics.json').write_text(json.dumps(metrics, indent=2)+'\n')
    return action, metrics


def check_runner(identity, timeout, process):
    """Check executable/options or endpoint before spending any calibration simulations."""
    import shutil
    if identity['runner'] == 'openai':
        models = [m.get('id') for m in openai_post(identity, '/models', None, min(15, timeout)).get('data', [])]
        if identity['model'] not in models:
            raise RuntimeError(f"model {identity['model']!r} not served; endpoint lists {models}")
        return dict(endpoint=identity['provider']['RAILSIM_OPENAI_BASE_URL'], served_models=models)
    if identity['runner'] == 'anthropic':
        import anthropic
        model = anthropic.Anthropic(timeout=min(15, timeout)).models.retrieve(identity['model'])
        return dict(sdk=anthropic.__version__, model=model.id, max_input_tokens=getattr(model, 'max_input_tokens', None))
    executable = shutil.which(identity['runner'])
    if not executable:
        raise RuntimeError(identity['runner'] + ' CLI not found on PATH')
    result = process([executable, '--version'], min(10, timeout),
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(identity['runner'] + ' --version failed')
    if identity['runner'] == 'copilot':
        help_result = process([executable, '--help'], min(10, timeout),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        required = ('--available-tools', '--disable-builtin-mcps', '--no-custom-instructions', '--no-ask-user')
        if help_result.returncode or any(flag not in help_result.stdout for flag in required):
            raise RuntimeError('Copilot CLI lacks required planner isolation options; update it before running')
    return dict(executable=executable, version=result.stdout.strip())


def main():
    import argparse
    import tempfile
    from .search import bounded_process
    parser = argparse.ArgumentParser(description='One model-only compatibility probe; no simulations.')
    parser.add_argument('--runner', choices=list(RUNNERS), default='copilot')
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    identity = model_identity(args.runner, args.model)
    installation = check_runner(identity, 15, bounded_process)
    schema = dict(type='object', additionalProperties=False, required=['action','volume','pattern','reason'], properties={
        'action':dict(type='string',enum=['random','rules','submit','stop']), 'volume':dict(type='integer'),
        'pattern':dict(type='array',items=dict(type='object',additionalProperties=False,required=['product','departure'],properties={
            'product':dict(type='string',enum=['FV','RV','GV']), 'departure':dict(type='integer')})), 'reason':dict(type='string')})
    with tempfile.TemporaryDirectory(prefix='railsim-model-probe-') as directory:
        action, metrics = request_action(identity,
            'Use no tools. Return action=random, volume=4, pattern=[], reason=compatibility probe.',
            schema, Path(directory)/'1', 120, bounded_process)
    if action['action'] != 'random' or action['volume'] != 4:
        raise RuntimeError('model did not follow compatibility probe')
    print(json.dumps(dict(installation=installation, action=action, metrics=metrics), indent=2))


if __name__ == '__main__':
    main()
