"""Explicit authored current-wire premises; never native replies or a fallback."""
import copy
import json
from pathlib import Path


def authored_support_reply(name):
    fixture = json.loads((Path(__file__).parent/'fixtures/authored_support_v7.json').read_text())
    assert fixture['provenance']['kind'] == 'synthetic_offline_contract_adaptation'
    reply = fixture['replies'][name]
    assert reply['version'] == 'summary-support-v7'
    return copy.deepcopy(reply)


def bind_report(row, target, anchors=(), properties=()):
    """Author a v7 fixture binding and its explicit dual-use citation roles."""
    selected = [ref['span_id'] for ref in row['citations']]
    for anchor in anchors:
        if anchor not in selected:
            selected.append(anchor)
    row['citations'] = [dict(span_id=ref, role='report_anchor' if ref in anchors else 'evidence')
                        for ref in selected]
    row['report_binding'] = dict(target=target, properties=list(properties))
