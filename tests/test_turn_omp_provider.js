// Test-only deterministic transport: no model service, socket or credentials.
import { AssistantMessageEventStream } from '@oh-my-pi/pi-ai';

export default function fixture(pi) {
  let requests = 0;
  pi.registerProvider('maven-fixture', {
    api: 'maven-fixture-api', baseUrl: 'https://unused.invalid/', apiKey: 'unused',
    models: [{id: 'commit-message', name: 'Offline turn probe', reasoning: false,
      input: ['text'], contextWindow: 32768, maxTokens: 2048,
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}}],
    streamSimple(model, context, options) {
      const stream = new AssistantMessageEventStream();
      const txt = msg => typeof msg.content === 'string' ? msg.content :
        (msg.content ?? []).filter(p => p.type === 'text').map(p => p.text).join('\n');
      const policy = context.messages.map(txt).find(text => text.includes('Leave changes uncommitted unless I explicitly ask'));
      const key = policy?.match(/"turn_key"\s*:\s*"([a-f0-9]{64}\.[a-f0-9]{32})"/)?.[1];
      const result = context.messages.filter(m => m.role === 'toolResult').at(-1);
      const receipt = result && txt(result);
      let content;
      ++requests;
      if (requests === 1 && key) {
        content = [{type: 'text', text: 'Draft: Respect manual Git commits.'}];
      } else if (requests === 2 && key && context.messages.some(m => txt(m).includes(`existing turn ${key}`))) {
        // The command is an OMP-mounted stdio MCP tool. Only the temporary
        // project is selected; neither the prompt nor the transport calls Git.
        const args = JSON.stringify({action: 'finish', turn_key: key, cwd: options.cwd ?? process.cwd()});
        const command = `mcp__maven_turn -j '${args.replaceAll("'", "'\\''")}'`;
        content = [{type: 'toolCall', id: 'verified-finish', name: 'bash', arguments: {i: 'Classifying unchanged turn', command}}];
      } else {
        const quoted = receipt?.match(/"footer"\s*:\s*("(?:[^"\\]|\\.)*")/)?.[1];
        const footer = quoted ? JSON.parse(quoted) : receipt?.match(/^MAVEN · .+$/m)?.[0];
        const ok = requests === 3 && key && !result?.isError && /consulted, no update/.test(footer ?? '');
        content = [{type: 'text', text: ok ? `Draft: Respect manual Git commits.\n${footer}` :
          'FAIL: no verified read-only MAVEN receipt.'}];
      }
      const call = content[0].type === 'toolCall';
      const message = {role: 'assistant', api: model.api, provider: model.provider, model: model.id,
        content, stopReason: call ? 'toolUse' : 'stop', timestamp: Date.now(), usage: {
          input: 1, output: 1, totalTokens: 2, cacheRead: 0, cacheWrite: 0,
          cost: {input: 0, output: 0, total: 0, cacheRead: 0, cacheWrite: 0}}};
      stream.push({type: 'start', partial: message});
      if (call) {
        stream.push({type: 'toolcall_start', contentIndex: 0, partial: message});
        stream.push({type: 'toolcall_delta', contentIndex: 0, delta: JSON.stringify(content[0].arguments), partial: message});
        stream.push({type: 'toolcall_end', contentIndex: 0, toolCall: content[0], partial: message});
      } else {
        stream.push({type: 'text_start', contentIndex: 0, partial: message});
        stream.push({type: 'text_delta', contentIndex: 0, delta: content[0].text, partial: message});
        stream.push({type: 'text_end', contentIndex: 0, content: content[0].text, partial: message});
      }
      stream.push({type: 'done', reason: message.stopReason, message});
      stream.end();
      return stream;
    },
  });
}
