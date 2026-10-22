"""Independent pure arithmetic for full versus known-reported usage."""
from decimal import Decimal
KEYS=('input_tokens','cached_input_tokens','output_tokens','total_tokens')
def cumulative_recount(prior,current):
 complete=prior['token_accounting_complete'] is True and current['token_accounting_complete'] is True
 bounds=[v.get('reported_tokens_lower_bound') or (v['tokens'] if v['token_accounting_complete'] else None) for v in (prior,current)]
 known=[v for v in bounds if v is not None]
 missing=sum(v.get('reported_tokens_lower_bound_missing_components',0 if b else 1) for v,b in zip((prior,current),bounds))
 out={k:prior[k]+current[k] for k in ('attempted_calls','finished_calls','failed_calls','unresolved_calls','calls_with_reported_tokens','calls_with_unknown_or_partial_tokens')}
 out.update(tokens={k:prior['tokens'][k]+current['tokens'][k] if complete else None for k in KEYS},token_accounting_complete=complete,
 reported_tokens_lower_bound={k:sum(b[k] for b in known) for k in KEYS} if known else None,
 reported_tokens_lower_bound_missing_components=missing,reported_tokens_lower_bound_coverage_complete=missing==0,cost_usd=None,
 pricing_complete=complete and prior['pricing_complete'] is True and current['pricing_complete'] is True)
 out['hypothetical_api_equivalent_usd']={tier:str(sum((Decimal(v['hypothetical_api_equivalent_usd'][tier]) if v['attempted_calls'] else Decimal(0) for v in (prior,current)),Decimal(0))) if all(not v['attempted_calls'] or v['hypothetical_api_equivalent_usd'][tier] is not None for v in (prior,current)) else None for tier in ('standard','fast')}
 return out
