"""Synthetic classification checks only; this cannot reproduce the remote failure."""
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from codex_mem.processor import ProcessorFailure, _TurnMonitor, _native_failure_reason

variants = ('httpConnectionFailed', 'responseStreamConnectionFailed',
            'responseStreamDisconnected', 'responseTooManyFailedAttempts')
cases = [('internalServerError', 'native_server_error'), ('serverOverloaded', 'native_server_error')]
for variant in variants:
    cases.extend(({variant: {'httpStatusCode': status}}, 'native_server_error') for status in range(500, 600))
controls = [('usageLimitExceeded', 'native_usage_limit'), ('rateLimitExceeded', 'native_rate_limit'),
    ({'httpConnectionFailed': {'httpStatusCode': 429}}, 'native_rate_limit'),
    ({'httpConnectionFailed': {'httpStatusCode': 401}}, 'native_auth'),
    ({'httpConnectionFailed': {'httpStatusCode': None}}, 'native_connection_error'),
    ({'httpConnectionFailed': {'httpStatusCode': '503'}}, 'native_turn_failed'),
    ({'httpConnectionFailed': {'httpStatusCode': True}}, 'native_turn_failed')]
for info, expected in cases + controls:
    monitor = _TurnMonitor('synthetic-thread')
    monitor.set_turn('synthetic-turn')
    turn = {'id': 'synthetic-turn', 'status': 'failed', 'items': [],
            'error': {'codexErrorInfo': info, 'message': 'DO_NOT_RETAIN_RAW_ERROR_PROSE'}}
    assert _native_failure_reason(turn) == expected
    try:
        monitor.observe({'method': 'turn/completed', 'params': {'threadId': 'synthetic-thread', 'turn': turn}})
    except ProcessorFailure as exc:
        assert exc.code == 'runner_failure' and exc.reason_code == expected
        assert 'DO_NOT_RETAIN_RAW_ERROR_PROSE' not in str(exc)
    else:
        raise AssertionError('synthetic failed turn was accepted')
    assert monitor.usage_receipt() == {'status': 'unavailable', 'source': 'app_server_thread_total',
                                     'updates': 0, 'tokens': None}
result = {'purpose': 'safe classification and information-loss controls only',
    'remote_call_reproduced': False, 'root_cause_proved': False,
    'native_server_error_shapes_checked': len(cases), 'nonserver_controls': len(controls),
    'server_error_routes': {'string_tags': ['internalServerError', 'serverOverloaded'],
                           'mapping_variants': list(variants), 'http_status_range': [500, 599]},
    'processor_sha256': hashlib.sha256((root / 'codex_mem/processor.py').read_bytes()).hexdigest(),
    'native_calls': 0, 'model_calls': 0, 'network_calls': 0}
output = Path(__file__).with_name('classification-probe.json')
output.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
