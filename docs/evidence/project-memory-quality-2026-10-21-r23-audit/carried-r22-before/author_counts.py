"""Strict selected R22 contents-free resource-count schema. No case reads."""
KINDS=('false_merge','false_closure','project_mixing','deleted_resurrection')
ROW_KEYS={'history_id','questions','required_facts','positive_anchors','typed_negatives','deletion_controls','observations','sessions','checkpoints'}
COUNT_KEYS={'histories','questions','required_facts','positive_anchors','typed_negatives','deletion_controls','observations','sessions','checkpoints','typed_negative_counts','maximum_event_characters','per_history'}
def validate_resource_shape(counts):
 if not isinstance(counts,dict) or set(counts)!=COUNT_KEYS:raise ValueError('author resource-count keys differ')
 for key in COUNT_KEYS-{'typed_negative_counts','per_history'}:
  if type(counts[key]) is not int or counts[key]<0:raise ValueError('invalid author resource scalar:'+key)
 negative=counts['typed_negative_counts']
 if not isinstance(negative,dict) or set(negative)!=set(KINDS) or any(type(v) is not int or v<0 for v in negative.values()):raise ValueError('invalid author typed-negative counts')
 rows=counts['per_history']
 if not isinstance(rows,list) or not rows:raise ValueError('author per_history must be nonempty list')
 identities=[]
 for row in rows:
  if not isinstance(row,dict) or set(row)!=ROW_KEYS or not isinstance(row['history_id'],str) or not row['history_id']:raise ValueError('invalid author per_history row')
  if any(type(row[k]) is not int or row[k]<0 for k in ROW_KEYS-{'history_id'}):raise ValueError('invalid author per_history scalar')
  identities.append(row['history_id'])
 if len(set(identities))!=len(identities) or counts['histories']!=len(rows):raise ValueError('author history coverage mismatch')
 for key in ROW_KEYS-{'history_id'}:
  if sum(row[key] for row in rows)!=counts[key]:raise ValueError('author row aggregate mismatch:'+key)
 if sum(negative.values())!=counts['typed_negatives']:raise ValueError('author negative aggregate mismatch')
 return counts

def derive_resources(fixture,gold):
 """Python len(event.text), including deletion events; nested captures excluded."""
 from collections import Counter
 rows=[];negative=Counter();texts=[]
 for history in fixture['histories']:
  events=[e for session in history['sessions'] for e in session['events']]
  if any(not isinstance(e.get('text'),str) for e in events):raise ValueError('selected R22 event requires string text')
  texts.extend(e['text'] for e in events)
  entries=[gold['questions'][history['id']+'/'+q['id']] for q in history['questions']]
  inferences=[x for entry in entries for x in entry['forbidden_inferences']]
  for item in inferences:
   if item['error_type'] not in KINDS:raise ValueError('unknown typed negative')
   negative[item['error_type']]+=1
  rows.append(dict(history_id=history['id'],questions=len(entries),required_facts=sum(len(e['required_facts']) for e in entries),positive_anchors=sum(len(f['support']) for e in entries for f in e['required_facts']),typed_negatives=len(inferences),deletion_controls=sum(e['type']=='delete_observation' for e in events),observations=sum(e['type']!='delete_observation' for e in events),sessions=len(history['sessions']),checkpoints=sum(len(s['checkpoints']) for s in history['sessions'])))
 if not texts:raise ValueError('selected R22 history requires events')
 result={key:sum(row[key] for row in rows) for key in ROW_KEYS-{'history_id'}}
 result.update(histories=len(rows),typed_negative_counts={key:negative[key] for key in KINDS},maximum_event_characters=max(map(len,texts)),per_history=rows)
 return validate_resource_shape(result)
