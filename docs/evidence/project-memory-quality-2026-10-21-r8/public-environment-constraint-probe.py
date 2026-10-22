"""Synthetic public API probe. Uses no trial fixtures, grades or models."""
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store


def probe():
    results = []
    for body, environment, source_kind in (
        ('No production deployment.', 'local', 'hook:UserPromptSubmit'),
        ('No production deployment.', '', 'hook:UserPromptSubmit'),
        ('Do not deploy to production; local validation only.', 'local', 'hook:UserPromptSubmit'),
        ('Production deployment completed.', 'production', 'hook:PostToolUse'),
    ):
        with TemporaryDirectory(prefix='public-environment-probe-') as directory:
            project = str((Path(directory) / 'register').resolve())
            with Store(Path(directory) / 'data') as store:
                configure(store.data_dir, project_memory_mode='pilot', capture_scope='all',
                          project_memory_projects=[project])
                source = store.remember(project, 'Synthetic source', body,
                    source=source_kind, session_id='evidence')
                memory = ProjectMemory(store)
                job = memory.claim(project)
                ref = {'source_id': source['id'], 'start': 0, 'end': len(body), 'quote': body}
                memory.commit(job, {'claims': [{'key': 'rule', 'text': body,
                    'kind': 'constraint' if source_kind.endswith('UserPromptSubmit') else 'result',
                    'workflow_state': 'open', 'scope': {'environment': environment},
                    'evidence': {'text': [ref]}}]})
                claim = memory.snapshot(project)['claims'][0]
                for query in ('Which constraints remain active in local?',
                              'Which constraints remain active in local, excluding production?'):
                    brief = build_project_brief(store, project, query=query, budget=6000)
                    context = store.context(project, query=query, budget=12000,
                                            allow_remote=False, include_health=False)
                    results.append({'body': body, 'source_role': claim['source_role'],
                        'source_field': 'body', 'span': [0, len(body)],
                        'declared_environment': environment, 'query': query,
                        'builder_source_visible': source['id'] in brief['xml'],
                        'store_context_source_visible': source['id'] in context})
    return results


if __name__ == '__main__':
    import json
    print(json.dumps(probe(), indent=2))
