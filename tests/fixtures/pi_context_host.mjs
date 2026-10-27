/** Offline host API harness. Loads the unchanged extension; never starts Pi or a model. */
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { SourceTextModule, SyntheticModule } from 'node:vm';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';

const source = process.argv[2];
const extension = new SourceTextModule(stripTypeScriptTypes(readFileSync(source, 'utf8')),
  { identifier: source, initializeImportMeta: (meta) => { meta.url = pathToFileURL(source).href; } });
await extension.link(async (name) => {
  // Pi itself supplies only VERSION at runtime. All local I/O remains real.
  const exports = name === '@earendil-works/pi-coding-agent' ? { VERSION: '1.4.0' } : await import(name);
  return new SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  });
});
await extension.evaluate();
const callbacks = new Map();
const entries = new Map();
let leaf = null;
let session = 'reader';
let messages = [{ role: 'custom', customType: 'other-extension', content: 'OTHER_CONTEXT' }];
const statuses = [];
const ctx = { cwd: process.argv[3], ui: { setStatus: (_key, value) => statuses.push(value) },
  sessionManager: { getSessionId: () => session, getLeafId: () => leaf, getEntry: (id) => entries.get(id) } };
extension.namespace.default({
  on: (event, callback) => callbacks.set(event, callback),
  appendEntry: (customType, data) => {
    const id = String(entries.size + 1);
    entries.set(id, { id, parentId: leaf, type: 'custom', customType, data }); leaf = id;
  },
  sendMessage: (message) => messages.push({ role: 'custom', ...message }),
});
for await (const line of createInterface({ input: process.stdin })) {
  const action = JSON.parse(line);
  if (action.operation === 'start') await callbacks.get('session_start')({}, ctx);
  if (action.operation === 'switch') {
    ctx.cwd = action.project; session = action.session; leaf = null;
    await callbacks.get('session_start')({}, ctx);
  }
  if (action.operation === 'prompt') {
    const result = await callbacks.get('before_agent_start')({ messageType: 'user', prompt: action.query }, ctx);
    if (result?.message) messages.push({ role: 'custom', ...result.message });
  }
  if (action.operation === 'shutdown') await callbacks.get('session_shutdown')({}, ctx);
  const provider = await callbacks.get('context')({ messages });
  process.stdout.write(JSON.stringify({ messages: provider.messages, statuses }) + '\n');
}
