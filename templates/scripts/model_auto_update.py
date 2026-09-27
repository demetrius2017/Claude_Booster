#!/usr/bin/env python3
"""Discover same-class replacements, qualify exact IDs, publish guarded receipts.

SessionStart launches refresh asynchronously; routes remain last-known-good until
qualification finishes. Catalog order/default is never treated as quality rank.
Only known model families are classified. Unknown families/providers are held.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

SUITE = 'routing-qualification-v1'
TTL = 86400


def state_path():
    return Path(os.environ.get('CLAUDE_MODEL_AUTO_UPDATE_STATE', str(Path.home() / '.claude/state/model_auto_update.json')))


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def family(provider, model):
    if not isinstance(model, str):
        return None
    if provider == 'codex-cli':
        match = re.fullmatch(r'gpt-(\d+(?:\.\d+)*?)-(astra|sol|terra|luna)', model)
        if match:
            return {'astra': 'flagship', 'sol': 'balanced', 'terra': 'balanced', 'luna': 'fast'}[match[2]]
    if provider == 'anthropic':
        match = re.fullmatch(r'claude-(opus|sonnet|haiku)-(\d+(?:-\d+)*)', model)
        if match:
            return match[1]
    return None


def version(model):
    return tuple(int(n) for n in re.findall(r'\d+', model))


def core(route):
    return {k: route.get(k) for k in ('provider', 'model', 'reasoning_effort')}


def fingerprint(route):
    return hashlib.sha256(json.dumps(core(route), sort_keys=True).encode()).hexdigest()


def eligible(route):
    return route.get('selection_mode', 'auto') == 'auto' and family(route.get('provider'), route.get('model', '')) is not None


def valid_receipt(route, receipt):
    if not isinstance(route, dict) or not eligible(route) or not isinstance(receipt, dict):
        return False
    target, checked = receipt.get('target'), receipt.get('checked_at')
    if not isinstance(target, dict) or type(checked) not in (int, float):
        return False
    if not math.isfinite(checked) or checked <= 0 or checked > time.time() + 60:
        return False
    return (receipt.get('suite') == SUITE and receipt.get('status') == 'passed'
            and isinstance(receipt.get('accepted_sources'), list)
            and fingerprint(route) in receipt['accepted_sources']
            and isinstance(target.get('model'), str)
            and version(target['model']) >= version(route['model'])
            and target.get('provider') == route.get('provider')
            and family(target.get('provider'), target['model']) == family(route['provider'], route['model'])
            and (route['provider'] != 'codex-cli' or target.get('reasoning_effort') == route.get('reasoning_effort')))


def apply_verified(decision):
    """Apply only receipts bound to the exact source route and current suite."""
    result = copy.deepcopy(decision)
    state = read_json(state_path())
    for route in result.get('routing', {}).values():
        if isinstance(route, dict):
            route.pop('auto_update_verified', None)
            route.pop('auto_update_checked_at', None)
    receipts = state.get('routes', {})
    if not isinstance(receipts, dict):
        return result
    for category, receipt in receipts.items():
        route = result.get('routing', {}).get(category)
        if not valid_receipt(route, receipt):
            continue
        target = receipt['target']
        route.update(target)
        route['auto_update_verified'] = True
        route['auto_update_checked_at'] = receipt['checked_at']
    if state:
        result['model_auto_update'] = {k: state.get(k) for k in ('checked_at', 'status', 'checks')}
    return result


def run(command, cwd, prompt, timeout=180):
    env = dict(os.environ, CLAUDE_MODEL_AUTO_UPDATE_CHILD='1')
    env.pop('CLAUDECODE', None)
    # Discovery must not inherit our last verified alias pin from the parent.
    for key in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL'):
        env.pop(key, None)
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError('probe_timeout')
    if process.returncode:
        # Diagnostics may contain credentials or account details: retain only class.
        raise RuntimeError('probe_process_failed')
    return (stdout, stderr) if command[0] == os.environ.get('CODEX_BIN', 'codex') else stdout


def codex_catalog():
    process = subprocess.Popen([os.environ.get('CODEX_BIN', 'codex'), 'app-server'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    responses = queue.Queue()
    def reader():
        for line in process.stdout:
            try:
                responses.put(json.loads(line))
            except ValueError:
                pass
        responses.put({'error': 'catalog_closed'})
    threading.Thread(target=reader, daemon=True).start()
    def request(number, method, params):
        process.stdin.write(json.dumps({'id': number, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            item = responses.get(timeout=max(.01, deadline - time.monotonic()))
            if item.get('id') == number:
                if 'error' in item:
                    raise RuntimeError('catalog_rpc_error')
                return item['result']
            if item.get('error') == 'catalog_closed':
                raise RuntimeError('catalog_closed')
        raise RuntimeError('catalog_timeout')
    try:
        request(1, 'initialize', {'clientInfo': {'name': 'booster-model-update', 'version': '1'}})
        process.stdin.write('{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        models, cursor, seen = [], None, set()
        for number in range(2, 22):
            params = {'limit': 100, 'includeHidden': False}
            if cursor:
                params['cursor'] = cursor
            page = request(number, 'model/list', params)
            models.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return models
            if cursor in seen:
                break
            seen.add(cursor)
        raise RuntimeError('catalog_pagination_incomplete')
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def choose_codex(route, models):
    candidates = []
    for entry in models:
        model = entry.get('model', '')
        if entry.get('hidden') or family('codex-cli', model) != family('codex-cli', route['model']):
            continue
        efforts = [e.get('reasoningEffort') for e in entry.get('supportedReasoningEfforts', [])]
        if route.get('reasoning_effort', 'medium') not in efforts:
            continue
        if version(model) > version(route['model']):
            candidates.append(model)
    return max(candidates, key=version) if candidates else None


def claude_call(model, cwd, prompt, tools=''):
    output = run([os.environ.get('CLAUDE_BIN', 'claude'), '-p', '--model', model,
                  '--output-format', 'json', '--no-session-persistence', '--strict-mcp-config',
                  '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
                  '--settings', '{"disableAllHooks":true}', '--tools', tools,
                  '--permission-mode', 'dontAsk', '--allowedTools', 'Read',
                  '--max-turns', '5'], cwd, prompt)
    data = json.loads(output)
    if data.get('is_error') or not isinstance(data.get('result'), str):
        raise RuntimeError('claude_result_error')
    identities = list(data.get('modelUsage', {}))
    # Ignore auxiliary Haiku accounting, but reject ambiguous main-model identity.
    identities = [m for m in identities if family('anthropic', m) == family('anthropic', model) or model in ('opus', 'sonnet', 'haiku') and family('anthropic', m) == model]
    if len(identities) != 1:
        raise RuntimeError('unverified_claude_identity')
    return data['result'], identities[0]


def qualify(provider, model, effort):
    """Independent fixture, arithmetic and patch reasoning; require actual file read.

    This is a compatibility/regression gate, not a claim of universal superiority.
    """
    with tempfile.TemporaryDirectory(prefix='booster-model-check-') as directory:
        nonce = os.urandom(16).hex()
        Path(directory, 'fixture.txt').write_text(nonce)
        prompt = ('Read fixture.txt with a file/shell tool. Return ONLY JSON with keys '
                  'nonce (file contents), total (sum of squares of odd integers from 1 through 9), '
                  'fix (correct Python expression replacing xs[1:] so sum covers ALL items of xs). '
                  'Do not modify files or use network.')
        if provider == 'anthropic':
            output, actual = claude_call(model, directory, prompt, 'Read')
            if actual != model:
                raise RuntimeError('model_identity_mismatch')
        else:
            result = Path(directory, 'answer.txt')
            _, diagnostics = run([os.environ.get('CODEX_BIN', 'codex'), 'exec', '--model', model,
                 '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral',
                 '-c', 'model_reasoning_effort=' + json.dumps(effort),
                 '-c', 'web_search="disabled"', '-o', str(result), '-'], directory, prompt)
            if not re.search(r'^model:\s*' + re.escape(model) + r'\s*$', diagnostics, re.MULTILINE):
                raise RuntimeError('unverified_codex_identity')
            output = result.read_text()
        text = output.strip()
        if text.startswith('```json\n') and text.endswith('```'):
            text = text[8:-3].strip()
        parsed = json.loads(text)
        if (not isinstance(parsed, dict) or set(parsed) != {'nonce', 'total', 'fix'}
                or parsed['nonce'] != nonce or parsed['total'] != 165
                or parsed['fix'] not in ('xs', 'xs[:]', 'xs[0:]', 'xs[::1]', 'xs[0::1]')):
            raise RuntimeError('qualification_answer_failed')
        return {'suite': SUITE, 'status': 'passed', 'checked_at': time.time()}


def refresh(decision, discover=codex_catalog, probe=qualify, resolve=None):
    state = read_json(state_path())
    receipts = state.get('routes', {})
    if not isinstance(receipts, dict):
        receipts = {}
    checks, models, discovery_error, cache = {}, None, None, {}
    def resolve_claude(alias):
        with tempfile.TemporaryDirectory(prefix='booster-model-discovery-') as directory:
            return claude_call(alias, directory, 'Reply OK.')[1]
    resolve = resolve or resolve_claude
    for category, route in decision.get('routing', {}).items():
        if not eligible(route):
            checks[category] = {'status': 'held', 'reason': 'pinned_or_unsupported_class'}
            continue
        provider, old = route['provider'], route['model']
        try:
            if provider == 'codex-cli':
                if models is None and discovery_error is None:
                    try:
                        models = discover()
                    except Exception as exc:
                        discovery_error = type(exc).__name__
                if discovery_error:
                    raise RuntimeError('catalog_unavailable')
                candidate = choose_codex(route, models)
            else:
                alias = family(provider, old)
                if any(r.get('provider') == provider and family(provider, r.get('model')) == alias and r.get('selection_mode') == 'pinned' for r in decision['routing'].values()):
                    raise RuntimeError('class_contains_explicit_pin')
                key = ('resolve', alias)
                if key not in cache:
                    try:
                        cache[key] = resolve(alias)
                    except Exception as exc:
                        cache[key] = exc
                if isinstance(cache[key], Exception):
                    raise RuntimeError('alias_discovery_failed')
                candidate = cache[key]
                if family(provider, candidate) != alias:
                    raise RuntimeError('candidate_class_mismatch')
                if version(candidate) <= version(old):
                    candidate = None
            if not candidate:
                if provider == 'anthropic' and not valid_receipt(route, receipts.get(category)):
                    candidate = old  # Qualify and freeze the initial alias baseline too.
                else:
                    checks[category] = {'status': 'current', 'model': old}
                    continue
            effort = route.get('reasoning_effort', 'medium')
            key = (provider, candidate, effort)
            if key not in cache:
                try:
                    cache[key] = probe(*key)
                except Exception as exc:
                    cache[key] = {'status': 'failed', 'reason': str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}
            evidence = cache[key]
            checks[category] = dict(evidence, previous=old, candidate=candidate)
            if evidence.get('status') != 'passed' or evidence.get('suite') != SUITE:
                continue
            target = dict(core(route), model=candidate)
            if provider != 'codex-cli':
                target.pop('reasoning_effort', None)
            previous = receipts.get(category, {})
            sources = [fingerprint(route), fingerprint(target)]
            if previous.get('target') == core(route) or fingerprint(previous.get('target', {})) == fingerprint(route):
                sources += previous.get('accepted_sources', [])
            receipts[category] = dict(evidence, target=target, previous=old, accepted_sources=list(dict.fromkeys(sources)))
        except Exception as exc:
            checks[category] = {'status': 'held', 'reason': str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}
    return {'schema_version': 1, 'checked_at': time.time(), 'status': 'checked', 'routes': receipts, 'checks': checks}


def publish(state, decision):
    """Publish Claude alias mappings before activating their exact-ID receipts.

    Preserve user env overrides; one alias cannot represent conflicting pinned
    versions. Re-read settings under a lock after slow probes finish.
    """
    settings_path = Path(os.environ.get('CLAUDE_MODEL_AUTO_UPDATE_SETTINGS', str(Path.home() / '.claude/settings.json')))
    previous = read_json(state_path())
    managed = previous.get('managed_aliases', {})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.with_suffix('.model-update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            if not isinstance(settings, dict) or not isinstance(settings.get('env', {}), dict):
                raise RuntimeError('invalid_claude_settings')
        else:
            settings = {}
        env = settings.setdefault('env', {})
        mappings = {}
        groups = {}
        for category, receipt in state.get('routes', {}).items():
            route = decision.get('routing', {}).get(category, {})
            if not valid_receipt(route, receipt):
                continue
            target = receipt['target']
            if target['provider'] == 'anthropic':
                alias = family('anthropic', target['model'])
                groups.setdefault(alias, []).append((category, target['model']))
        for alias, entries in groups.items():
            key = 'ANTHROPIC_DEFAULT_' + alias.upper() + '_MODEL'
            targets = {model for _, model in entries}
            pinned = {r['model'] for r in decision['routing'].values() if r.get('provider') == 'anthropic' and family('anthropic', r.get('model')) == alias and r.get('selection_mode') == 'pinned'}
            if len(targets) != 1 or pinned - targets or env.get(key) not in (None, managed.get(key), next(iter(targets))):
                for category, _ in entries:
                    state['checks'][category] = {'status': 'held', 'reason': 'alias_mapping_conflict'}
                    state['routes'].pop(category, None)
                continue
            mappings[key] = next(iter(targets))
        # Freeze existing concrete Claude routes even when their baseline check
        # failed. This preserves current behavior and prevents an unqualified
        # future alias release; it does not mark that route verified.
        baselines = {}
        for route in decision.get('routing', {}).values():
            if route.get('provider') == 'anthropic':
                alias = family('anthropic', route.get('model'))
                if alias:
                    baselines.setdefault(alias, set()).add(route['model'])
        for alias, values in baselines.items():
            key = 'ANTHROPIC_DEFAULT_' + alias.upper() + '_MODEL'
            if len(values) == 1 and key not in env and key not in mappings:
                mappings[key] = next(iter(values))
        if mappings:
            env.update(mappings)
            atomic(settings_path, settings)
        state['managed_aliases'] = dict(managed, **mappings)
        atomic(state_path(), state)


def launch():
    if os.environ.get('CLAUDE_MODEL_AUTO_UPDATE_CHILD') == '1' or os.environ.get('CLAUDE_MODEL_AUTO_UPDATE_DISABLE') == '1':
        return
    if time.time() - read_json(state_path()).get('checked_at', 0) < TTL:
        return
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'refresh'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['start', 'refresh', 'status'])
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.command == 'start':
        launch()
        return
    if args.command == 'status':
        print(json.dumps(read_json(state_path()), indent=2))
        return
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if not args.force and time.time() - read_json(path).get('checked_at', 0) < TTL:
            return
        os.environ['CLAUDE_MODEL_AUTO_UPDATE_CHILD'] = '1'
        import model_balancer
        prior = read_json(path)
        atomic(path, dict(prior, status='checking', started_at=time.time(), pid=os.getpid()))
        try:
            decision = model_balancer.current_decision()
            state = refresh(decision)
            with model_balancer._BALANCER_PATH.with_suffix('.lock').open('a') as routing_lock:
                fcntl.flock(routing_lock, fcntl.LOCK_EX)
                latest = model_balancer._with_default_routes(model_balancer._load_file() or decision)
                publish(state, latest)
                model_balancer._decide_unlocked(force=True)
        except Exception as exc:
            state = dict(read_json(path), status='failed', checked_at=time.time(), error=type(exc).__name__)
            atomic(path, state)
            print(json.dumps({'status': 'failed', 'error': type(exc).__name__}), file=sys.stderr)
            return 1
        print(json.dumps(state, indent=2))
        return 0


if __name__ == '__main__':
    sys.exit(main())
