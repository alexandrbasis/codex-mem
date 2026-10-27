// Trusted explicit Pi extension. No tools, output handlers, routing or model calls.
import { createHash } from 'node:crypto';
import { readFileSync, appendFileSync } from 'node:fs';

const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');

export default function schemaReview(pi) {
  const config = JSON.parse(readFileSync(process.env.CODEX_MEM_PI_SCHEMA_CONTRACT, 'utf8'));
  const schemaBytes = readFileSync(config.schema_path);
  const helperSha = sha(readFileSync(config.helper_path));
  const schemaSha = sha(schemaBytes);
  const schema = JSON.parse(schemaBytes.toString('utf8'));
  let applications = 0;
  pi.on('before_provider_request', (event, ctx) => {
    applications += 1;
    const record = (value) => appendFileSync(config.proof_path, JSON.stringify(value) + '\n', { mode: 0o600 });
    const payload = event.payload;
    const valid = applications === 1 && config.version === 'pi-review-schema-v1' &&
      schemaSha === config.schema_sha256 && helperSha === config.helper_sha256 &&
      payload && typeof payload === 'object' && !Array.isArray(payload) &&
      payload.model === config.model && ctx.model?.id === config.model &&
      ctx.model?.provider === config.provider && payload.text &&
      typeof payload.text === 'object' && !Array.isArray(payload.text) &&
      !('format' in payload.text) &&
      (payload.tools === undefined || (Array.isArray(payload.tools) && payload.tools.length === 0));
    if (!valid) {
      record({ status: 'invalid', application: applications });
      // Pi catches hook errors. Python requires exact applied proof before acceptance.
      throw new Error('codex_mem_schema_contract_invalid');
    }
    const next = { ...payload, text: { ...payload.text,
      format: { type: 'json_schema', name: config.name, strict: true, schema } } };
    const restored = { ...next, text: { ...next.text } };
    delete restored.text.format;
    record({ status: 'applied', application: applications, helper_sha256: helperSha,
      schema_sha256: schemaSha, provider: ctx.model.provider, model: payload.model,
      before_payload_sha256: sha(JSON.stringify(payload)),
      restored_payload_sha256: sha(JSON.stringify(restored)),
      modified_keys: ['text.format'], format_name: config.name, strict: true, tools_count: 0 });
    return next;
  });
}
