"""Consumer acceptance: verified Claude versions and future Codex telemetry."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'templates/scripts'))
import model_tag_enforcer as enforcer
import model_metric_capture as metrics


class VerifiedClaudeRoute(unittest.TestCase):
    def call(self, model, managed=True, effort='medium', alias_target='claude-opus-9', nested=False):
        route = {'provider': 'anthropic', 'model': 'claude-opus-9'}
        if managed:
            route['auto_update_verified'] = True
        payload = {'tool_name': 'Agent', 'tool_input': {
            'description': '[opus] [coding] Implement feature',
            'subagent_type': 'general-purpose',
        }}
        if model is not None:
            payload['tool_input']['model'] = model
        with patch.object(enforcer, '_load_routing', return_value={'coding': route}), \
             patch.object(enforcer, 'is_subagent_context', return_value=nested), \
             patch.object(enforcer, 'append_jsonl'), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.dict(enforcer.os.environ, {enforcer._SKIP_ENV: '0', 'CLAUDE_EFFORT': effort, 'ANTHROPIC_DEFAULT_OPUS_MODEL': alias_target}), \
             contextlib.redirect_stderr(io.StringIO()):
            return enforcer.main()

    def test_verified_exact_version_launches(self):
        self.assertEqual(self.call('opus'), 0)
        self.assertEqual(self.call('opus', nested=True), 0)

    def test_alias_inherit_other_version_and_stronger_unverified_blocked(self):
        for model in ('inherit', None, 'claude-opus-9', 'claude-opus-8', 'claude-fable-9'):
            with self.subTest(model=model):
                self.assertEqual(self.call(model), 2)
                self.assertEqual(self.call(model, effort='max'), 2)

    def test_missing_or_stale_session_mapping_blocks_alias_even_nested(self):
        for mapping in ('', 'claude-opus-8', 'opus'):
            for nested in (False, True):
                self.assertEqual(self.call('opus', alias_target=mapping, nested=nested), 2)

    def test_unmanaged_manual_routes_keep_tier_behavior(self):
        self.assertEqual(self.call('opus', managed=False), 0)
        self.assertEqual(self.call('haiku', managed=False), 2)


class FutureCodexTelemetry(unittest.TestCase):
    def test_future_versioned_ids_collected(self):
        for model in ('gpt-7-astra', 'gpt-6.2-sol', 'gpt-10-luna', 'gpt-5.3-codex-spark'):
            for command in (f'codex_worker.sh {model}', f'codex exec -m {model}'):
                self.assertEqual(metrics._match_codex_command(command), model)

    def test_future_model_wrapper_provenance_is_preserved(self):
        row = {
            'event': 'codex_route', 'requested_model': 'gpt-7-astra',
            'effective_model': 'gpt-7-astra', 'reason': 'requested',
            'source': 'balancer', 'category': 'hard', 'cache_age_seconds': None,
            'attempts': [{'model': 'gpt-7-astra', 'success': True, 'duration_ms': 5}],
        }
        event = {'tool_response': {'stderr': 'codex_worker: ' + json.dumps(row)}}
        self.assertEqual(metrics._codex_provenance(event, 'gpt-7-astra'), row)

    def test_unrelated_and_malformed_ids_rejected(self):
        for model in ('gpt-next', 'gpt-6--astra', 'gpt-6.', 'claude-opus-9', None):
            self.assertFalse(metrics._is_codex_model(model))


if __name__ == '__main__':
    unittest.main()
