"""Neutral source custody, independent of event processing and claim authority.

Rows contain identities and offsets only. Reading custody never infers user intent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pm_intent_custody ("
        "id TEXT PRIMARY KEY,project TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,"
        "session_id TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,seq INTEGER NOT NULL,"
        "salient INTEGER NOT NULL DEFAULT 0,claim_id TEXT REFERENCES pm_objects(id) ON DELETE SET NULL,"
        "UNIQUE(project,source_id,start,end))")
    connection.execute("CREATE INDEX IF NOT EXISTS pm_custody_project ON pm_intent_custody(project,seq,source_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS pm_custody_source ON pm_intent_custody(source_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS pm_custody_transition_target ON pm_relations(project,to_id,type)")


def record_consumed(connection: sqlite3.Connection, project: str, source: dict) -> None:
    """Capture fresh consumed user fragments atomically; never backfill history."""
    if source.get('historical') or source.get('source_role') != 'user_intent' or not source['body']:
        return
    row = connection.execute(
        "SELECT v.seq,v.salient,e.session_id FROM pm_events v JOIN entries e ON e.id=v.source_id "
        "WHERE v.project=? AND e.project=v.project AND v.source_id=?", (project, source['id'])).fetchone()
    if row is None:
        return
    start, end = source['body_offset'], source['body_offset'] + len(source['body'])
    identity = 'pmc_' + hashlib.sha256(json.dumps([project, source['id'], start, end]).encode()).hexdigest()[:40]
    connection.execute("INSERT OR IGNORE INTO pm_intent_custody(id,project,source_id,session_id,start,end,seq,salient) "
                       "VALUES (?,?,?,?,?,?,?,?)", (identity, project, source['id'], row['session_id'] or '',
                                                  start, end, row['seq'], row['salient']))


def associate_claims(connection: sqlite3.Connection, project: str, objects: list[dict]) -> None:
    """Only whole-source exact-text claims can discharge neutral source custody.

    Evidence overlap, paraphrases and partial compound clauses establish no
    complete representation. Fragmented sources stay unresolved conservatively.
    """
    for obj in objects:
        if (obj['object_type'] != 'claim' or obj.get('kind') not in {'requirement', 'constraint', 'task'}
                or obj.get('workflow_state') not in {'open', 'blocked', 'accepted'} or obj.get('requested_state')):
            continue
        for ref in obj.get('evidence', {}).get('text', []):
            if ref.get('source_role') != 'user_intent' or ref.get('quote') != obj['text']:
                continue
            connection.execute(
                "UPDATE pm_intent_custody SET claim_id=? WHERE project=? AND source_id=? AND claim_id IS NULL "
                "AND start=0 AND end=? AND EXISTS (SELECT 1 FROM entries e WHERE e.project=? "
                "AND e.id=source_id AND length(e.body)=end AND e.body=?)",
                (obj['id'], project, ref['source_id'], ref['end'], project, obj['text']))


def _projection(exclude_session_id: str | None) -> tuple[str, tuple]:
    # Apply the same session exclusion to source, represented claim, and its
    # transition evidence. A hidden-session transition must not change fate.
    predicate = ("r.project=c.project AND r.to_id=c.claim_id AND (? IS NULL OR NOT EXISTS "
                 "(SELECT 1 FROM pm_relation_evidence re JOIN entries e ON e.id=re.source_id "
                 "WHERE re.relation_id=r.id AND e.session_id=?))")
    state = ("CASE WHEN c.claim_id IS NULL OR (? IS NOT NULL AND EXISTS "
             "(SELECT 1 FROM pm_evidence pe JOIN entries e ON e.id=pe.source_id "
             "WHERE pe.object_id=c.claim_id AND e.session_id=?)) THEN 'unresolved' "
             "WHEN EXISTS (SELECT 1 FROM pm_relations r WHERE " + predicate + " AND r.type='replaces') "
             "THEN 'unresolved' WHEN EXISTS (SELECT 1 FROM pm_relations r WHERE " + predicate +
             " AND r.type='closes') THEN 'completed' ELSE 'stored' END")
    return state, (exclude_session_id,) * 6


def snapshot(connection: sqlite3.Connection, project: str, limit: int,
             exclude_session_id: str | None = None, *, fts_query: str = "") -> tuple[list[dict], dict]:
    state, args = _projection(exclude_session_id)
    base = ("SELECT c.*," + state + " disposition FROM pm_intent_custody c WHERE c.project=? "
            "AND (? IS NULL OR c.session_id!=?)")
    parameters = (*args, project, exclude_session_id, exclude_session_id)
    counts = {name: 0 for name in ('stored', 'completed', 'cancelled', 'unresolved')}
    for row in connection.execute("SELECT disposition,COUNT(*) n FROM (" + base + ") GROUP BY disposition", parameters):
        counts[row['disposition']] = row['n']
    selected = base
    selected_parameters = parameters
    if fts_query:
        # entries_fts is external-content. Resolve its rowids through entries,
        # before applying the custody candidate limit, without reading bodies.
        selected += (" AND c.source_id IN (SELECT e.id FROM entries e WHERE e.project=? "
                     "AND e.rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?))")
        selected_parameters = (*parameters, project, fts_query)
    matched = connection.execute("SELECT COUNT(*) FROM (" + selected + ")", selected_parameters).fetchone()[0]
    rows = connection.execute(
        "SELECT q.*,substr(e.body,q.start+1,min(q.end-q.start,1200)) excerpt FROM (" + selected +
        " ORDER BY CASE disposition WHEN 'unresolved' THEN 0 ELSE 1 END,salient DESC,seq,start,id LIMIT ?) q "
        "JOIN entries e ON e.id=q.source_id AND e.project=q.project", (*selected_parameters, limit))
    values = []
    for row in rows:
        transition_ids = []
        if row['disposition'] == 'completed':
            transition_ids = [r[0] for r in connection.execute(
                "SELECT r.id FROM pm_relations r WHERE r.project=? AND r.to_id=? AND r.type='closes' "
                "AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM pm_relation_evidence re "
                "JOIN entries e ON e.id=re.source_id WHERE re.relation_id=r.id AND e.session_id=?)) "
                "ORDER BY r.id LIMIT 9", (project, row['claim_id'], exclude_session_id, exclude_session_id))]
        values.append({key: row[key] for key in ('id', 'project', 'source_id', 'session_id', 'start', 'end',
                                                'seq', 'salient', 'disposition')} |
                      {'claim_id': row['claim_id'] if row['disposition'] != 'unresolved' else None,
                       'transition_ids': transition_ids[:8], 'transition_pointers_omitted': len(transition_ids) > 8,
                       'text': row['excerpt'], 'truncated': row['end'] - row['start'] > 1200,
                       'source_role': 'user_intent', 'is_requirement_claim': False,
                       'meaning': 'original_user_source_with_unreconciled_representation',
                       'uncertainty': 'extracted_representation_completeness_unresolved' if row['disposition'] == 'unresolved' else None})
    total = sum(counts.values())
    return values, dict(counts, total=total, matched=matched, unmatched=total-matched,
                        matched_omitted=matched-len(values), matched_complete=matched == len(values),
                        query_filtered=bool(fts_query), returned=len(values), omitted=total-len(values),
                        complete=total == len(values), automatic_backfill=False,
                        representation_rule='whole_source_exact_text_only', cancellation_inference=False)


def protected_source_ids(connection: sqlite3.Connection, project: str) -> set[str]:
    state, args = _projection(None)
    return {row[0] for row in connection.execute(
        "SELECT DISTINCT source_id FROM (SELECT c.source_id," + state +
        " disposition FROM pm_intent_custody c WHERE c.project=?) WHERE disposition='unresolved'", (*args, project))}
