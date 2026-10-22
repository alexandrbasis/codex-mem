"""Disjoint lexical XML transport costs of saved disclosed views; no retrieval."""
import collections
import hashlib
import html
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
BASE=Path(__file__).resolve().parent

def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def nodecost(xml):
    tags=re.findall(r'<[^>]*>',xml); quote=re.findall(r'<quote_excerpt>(.*?)</quote_excerpt>',xml,re.S);derived=re.findall(r'<derived_text>(.*?)</derived_text>',xml,re.S)
    text=''.join(re.split(r'<[^>]*>',xml));source_tags=[t for t in tags if re.match(r'</?source(?:\s|>)',t)];field_tags=[t for t in tags if re.match(r'</?field(?:\s|>)',t)];item_tags=[t for t in tags if re.match(r'</?item(?:\s|>)',t)];execution=[t for t in tags if t.startswith('<execution ')];attrs=collections.Counter();attrbytes=collections.Counter()
    for t in tags:
        for name,value in re.findall(r'\s+([\w:-]+)="([^"]*)"',t):attrs[name]+=1;attrbytes[name]+=len(' '+name+'="'+value+'"')
    return {'chars':len(xml),'tag_and_attribute_chars':sum(map(len,tags)),'all_text_chars':len(text),'quote_text_xml_chars':sum(map(len,quote)),'quote_text_decoded_chars':sum(len(html.unescape(q))for q in quote),'derived_text_xml_chars':sum(map(len,derived)),'source_tags_chars':sum(map(len,source_tags)),'field_tags_chars':sum(map(len,field_tags)),'item_tags_chars':sum(map(len,item_tags)),'execution_tag_chars':sum(map(len,execution)),'attribute_chars_by_name':dict(attrbytes),'attribute_counts':dict(attrs)}

def partcost(key,xml,losses,budget):
    root=ET.fromstring(xml);items=[];intervals=collections.Counter();quotes=collections.Counter();redundancy=[]
    for element in root.iter('source'):
        identity=(element.get('source_id'),element.get('source_field','body'),element.get('source_role'),element.get('start'),element.get('end'));intervals[identity]+=1
        q=element.find('quote_excerpt')
        if q is not None:quotes[(identity,''.join(q.itertext()))]+=1
    for match in re.finditer(r'<item\b[^>]*(?:/>|>.*?</item>)',xml,re.S):
        raw=match.group();element=ET.fromstring(raw);sources=[{'source_id':e.get('source_id'),'field':e.get('source_field','body'),'role':e.get('source_role'),'start':e.get('start'),'end':e.get('end')}for e in element.iter('source')];items.append({'id':element.get('id'),'lane':element.get('lane'),'source_role':element.get('source_role'),'execution':element.find('execution')is not None,'sources':sources,**nodecost(raw)})
    # Exact lexical repeated attribute values are candidates, not permission to erase qualifiers.
    for name in ('worktree','capture_order_basis','quote_truncated','source_role','basis','field_association'):
        seen=collections.Counter(re.findall(r'\s'+name+r'="([^"]*)"',xml));redundancy.append({'attribute':name,'repeated_exact_value_chars_after_first':sum((n-1)*len(' '+name+'="'+v+'"')for v,n in seen.items()if n>1),'values':dict(seen)})
    candidate_savings=sum(r['repeated_exact_value_chars_after_first'] for r in redundancy)
    matched=[]
    for loss in losses:
        source=loss['new'];body=source['body'];encoded=html.escape(body,quote=False);attrs=' source_id="'+source['source_id']+'" source_field="body" source_role="user_intent" start="0" end="'+str(len(body))+'"';minimal='<source'+attrs+'><quote_excerpt>'+encoded+'</quote_excerpt></source>';matched.append({'source_id':source['source_id'],'event_id':source['event_id'],'body_chars':len(body),'exact_whole_source_minimal_transport_chars':len(minimal),'budget_free_chars':budget-len(xml),'fits_free_capacity_without_removal':len(minimal)<=budget-len(xml),'lexical_repeated_attribute_candidate_savings':candidate_savings,'fits_if_all_candidate_attributes_shared':len(minimal)<=budget-len(xml)+candidate_savings,'caution':'Minimal transport excludes enclosing field/item scope uncertainty/status metadata; necessary lower bound only.'})
    root_tags=re.findall(r'</?project-memory\b[^>]*>',xml);coverage=re.findall(r'<(?:coverage|omitted|source-context-coverage)\b[^>]*>',xml)
    return {'project_key':key,'owner_budget':budget,**nodecost(xml),'root_tags_chars':sum(map(len,root_tags)),'coverage_and_omission_marker_chars':sum(map(len,coverage)),'unique_source_intervals':len(intervals),'source_interval_occurrences':sum(intervals.values()),'duplicate_interval_occurrences':sum(n-1 for n in intervals.values()),'exact_duplicate_quote_text_xml_chars':sum((n-1)*len(html.escape(text,quote=False))for (ident,text),n in quotes.items()),'repeated_attribute_candidates':redundancy,'item_class_transport_chars':{label:sum(i['chars'] for i in items if (i['execution'] if label=='execution' else i['source_role']=='assistant_report' if label=='assistant_report' else any(s['role']=='user_intent' for s in i['sources']))) for label in ('execution','assistant_report','user_source_bearing')},'items_largest_first':sorted(items,key=lambda x:-x['chars']),'lost_user_source_lower_bound_costs':matched}

def main():
    files={'v6':BASE.parent/'project-memory-quality-2026-10-21-r14/disclosed-replay-v6/new-contexts.json','v2':BASE/'disclosed-replay-v2/new-contexts.json'};data={k:{x['case']:x['new_context']for x in read(p)}for k,p in files.items()};comparison=read(BASE/'replay-comparison-v2.json');losses=comparison['user_source_coverage']['complete_losses'];cases=[]
    for case in sorted(data['v2']):
        if not(case.endswith('/q5')or case=='hold_1011/q6'):continue
        entry={'case':case,'views':{}}
        for label,views in data.items():
            context=views[case];parts=re.findall(r'PROJECT ([^\n]+)\n(<project-memory\b.*?</project-memory>)',context['memory'],re.S);budget=max(1000,6000//len(parts)-100);entry['views'][label]={'context_sha256':context['context_sha256'],'total_context_chars':len(context['memory']),'parts':[partcost(key,xml,[v for v in losses if v['new']['case']==case and v['new']['project_key']==key],budget)for key,xml in parts]}
        cases.append(entry)
    out={'status':'saved_xml_structural_cost_only_no_semantic_regrading','models_called':0,'runtime_calls':0,'input_hashes':{str(p):sha(p)for p in files.values()},'cases':cases,'method':['Character accounting matches the renderer budget; UTF-8 bytes can differ and are not the budget.','Tag/attribute and all-text counts partition each XML part; quote/derived/source/field/item/execution counts are nested explanatory subsets, not additive.','Exact duplicate quote text requires same source ID/field/role/interval and identical decoded text.','Repeated attributes are only lexical sharing candidates: scope/role/uncertainty cannot be dropped without explicit inheritance/reference semantics.','Transport lower bounds for omitted originals exclude required record qualifiers; fitting a lower bound does not prove actual group fits.','No claim that all missing user sources are relevant or mandatory.']}
    totals={}
    for label in files:
        parts=[p for c in cases for p in c['views'][label]['parts']];totals[label]={k:sum(p[k]for p in parts)for k in ('chars','tag_and_attribute_chars','all_text_chars','quote_text_xml_chars','derived_text_xml_chars','source_tags_chars','execution_tag_chars','root_tags_chars','coverage_and_omission_marker_chars','duplicate_interval_occurrences','exact_duplicate_quote_text_xml_chars')};totals[label]['lexical_repeated_attributes_candidate_chars']=sum(r['repeated_exact_value_chars_after_first']for p in parts for r in p['repeated_attribute_candidates'])
    out['totals']=totals;path=BASE/'render-cost-diagnosis-v2.json';path.write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(totals));print('sha',sha(path))
if __name__=='__main__':main()
