"""Explicit authored current-wire premises; never native replies or a fallback."""
import copy
import json
from pathlib import Path


def authored_support_reply(name):
    fixture = json.loads((Path(__file__).parent/'fixtures/authored_support_v6.json').read_text())
    assert fixture['provenance']['kind'] == 'synthetic_offline_contract_adaptation'
    reply = fixture['replies'][name]
    assert reply['version'] == 'summary-support-v6'
    return copy.deepcopy(reply)
