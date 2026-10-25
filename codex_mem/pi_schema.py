"""Attempt-local native schema data and an acceptance fence for one trusted hook.

Pi swallows hook errors. Proof checks deny acceptance, not already-sent requests.
The helper only modifies text.format; no credentials or provider clients live here.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

HELPER_PATH = Path(__file__).with_name('pi_schema_helper.js')
HELPER_SHA256 = '7523f97e2c93543666b8caaa4bcaa448efbbf5d4f3364d8b64a4b178f9979040'
VERSION = 'pi-review-schema-v1'
NAME = 'codex_mem_review_v1'


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _fail(code):
    from .processor import ProcessorFailure
    raise ProcessorFailure('invalid_response', reason_code=code)


class SchemaContract:
    """One schema and helper application owned by one ephemeral review process."""
    def __init__(self, directory, schema, profile):
        self.directory = Path(directory)
        self.profile = profile
        self.status = 'not_applied'
        try:
            helper = HELPER_PATH.read_bytes()
        except OSError:
            _fail('pi_schema_helper_invalid')
        if _digest(helper) != HELPER_SHA256:
            _fail('pi_schema_helper_invalid')
        self.helper = self.directory / 'schema-helper.js'
        self.schema = self.directory / 'review-schema.json'
        self.config = self.directory / 'schema-contract.json'
        self.proof = self.directory / 'schema-proof.jsonl'
        raw_schema = (json.dumps(dict(schema), sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        self.schema_sha256 = _digest(raw_schema)
        self.helper.write_bytes(helper)
        self.schema.write_bytes(raw_schema)
        config = dict(version=VERSION, name=NAME, model=profile.model, provider=profile.provider,
            helper_path=str(self.helper), schema_path=str(self.schema), proof_path=str(self.proof),
            helper_sha256=HELPER_SHA256, schema_sha256=self.schema_sha256)
        self.config.write_text(json.dumps(config, sort_keys=True))
        self.config_sha256 = _digest(self.config.read_bytes())

    def configure(self, environment):
        # The caller owns this fresh profile. Never read global or project settings.
        settings_path = self.directory / 'agent' / 'settings.json'
        settings = json.loads(settings_path.read_text())
        settings.update(transport='sse', cacheWarming='off', compaction=dict(enabled=False),
                        retry=dict(enabled=False, provider=dict(maxRetries=0)))
        settings_path.write_text(json.dumps(settings))
        environment['CODEX_MEM_PI_SCHEMA_CONTRACT'] = str(self.config)

    def verify_files(self):
        try:
            valid = (_digest(self.helper.read_bytes()) == HELPER_SHA256
                     and _digest(self.schema.read_bytes()) == self.schema_sha256
                     and _digest(self.config.read_bytes()) == self.config_sha256)
        except OSError:
            valid = False
        if not valid:
            self.status = 'invalid'
            _fail('pi_schema_proof_invalid')

    def verify(self):
        from .processor import ProcessorFailure
        self.verify_files()
        try:
            with self.proof.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('proof bounds')
            # Same strict JSON decoder as native replies: duplicate keys stay invalid.
            from .pi_runtime import _json
            lines = raw.decode().splitlines()
            if len(lines) != 1:
                raise ValueError('proof count')
            proof = _json(lines[0])
            expected = dict(status='applied', application=1, helper_sha256=HELPER_SHA256,
                schema_sha256=self.schema_sha256, provider=self.profile.provider, model=self.profile.model,
                modified_keys=['text.format'], format_name=NAME, strict=True, tools_count=0)
            valid = set(proof) == {*expected, 'before_payload_sha256', 'restored_payload_sha256'}
            valid = valid and all(proof[key] == value and type(proof[key]) is type(value) for key, value in expected.items())
            before = proof.get('before_payload_sha256')
            valid = (valid and isinstance(before, str) and re.fullmatch(r'[0-9a-f]{64}', before)
                     and before == proof.get('restored_payload_sha256'))
        except (OSError, ValueError, UnicodeError, ProcessorFailure):
            valid = False
        if not valid:
            self.status = 'invalid'
            _fail('pi_schema_proof_invalid')
        self.status = 'applied'

    def receipt(self):
        return dict(mode='strict_json_schema', version=VERSION, status=self.status,
                    helper_sha256=HELPER_SHA256, schema_sha256=self.schema_sha256,
                    transport='sse', cache_warming='off', provider_retries=0)
