// Test-only deterministic transport: no model service, socket or credentials.
import { AssistantMessageEventStream } from '@oh-my-pi/pi-ai';

export default function fixture(pi) {
  pi.registerProvider('maven-fixture', {
    api: 'maven-fixture-api', baseUrl: 'https://unused.invalid/', apiKey: 'unused',
    models: [{id: 'commit-message', name: 'Offline turn probe', reasoning: false,
      input: ['text'], contextWindow: 32768, maxTokens: 2048,
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}}],
    streamSimple(model, context, options) {
      const stream = new AssistantMessageEventStream();
      const txt = msg => typeof msg.content === 'string' ? msg.content :
        (msg.content ?? []).filter(p => p.type === 'text').map(p => p.text).join('\n');
      const lastUser = context.messages.findLastIndex(m => m.role === 'user');
      const prompt = txt(context.messages[lastUser]);
      const results = context.messages.filter(m => m.role === 'toolResult');
      const view = results.map(txt).find(s => s.includes('Leave changes uncommitted unless I explicitly ask'));
      const latest = context.messages.slice(lastUser + 1).findLast(m => m.role === 'toolResult');
      const callTool = (name, args) => [{type: 'toolCall', id: name, name: 'bash', arguments: {
        i: 'Using local fixture memory', command: `mcp__${name} -j '${JSON.stringify(args).replaceAll("'", "'\\''")}'`}}];
      const cwd = options.cwd ?? process.cwd();
      let response = 'FAIL: missing policy, context or confirmed local save.';
      let content;
      if (prompt.includes('Draft only') && String(context.systemPrompt).includes('Every turn')) {
        if (!latest) content = callTool('maven_context', {cwd});
        else if (view) response = 'Draft: Respect manual Git commits.\nMAVEN · widget: no local change.';
      } else if (prompt.includes('Should you commit') && view) {
        response = 'No; commits are manual.\nMAVEN · widget: no local change.';
      } else if (prompt.includes('Remember the widget') && view) {
        const id = view.match(/"project_id"\s*:\s*"([^"]+)"/)?.[1];
        const token = view.match(/"token"\s*:\s*"([^"]+)"/)?.[1];
        if (!latest && id && token) content = callTool('maven_local_write', {cwd, project_id: id,
          expected_token: token, kind: 'decision', data: {title:'Build the widget offline',state:'accepted'},
          reason: 'Owner requested the widget decision.'});
        else if (latest && !latest.isError && /"changed"\s*:\s*true/.test(txt(latest)))
          response = 'Recorded the widget decision.\nMAVEN · widget: saved locally.';
      } else if (prompt.includes('Record the second decision') && view) {
        const saved = results.map(txt).findLast(s => /"changed"\s*:\s*true/.test(s));
        if (!latest && saved) content = callTool('maven_local_write', {cwd,
          project_id: view.match(/"project_id"\s*:\s*"([^"]+)"/)[1],
          expected_token: saved.match(/"token"\s*:\s*"([^"]+)"/)[1], kind:'decision',
          data:{title:'Competing change', state:'accepted'}, reason:'Second fixture request.'});
        else if (latest && txt(latest).includes('view_conflict'))
          response = 'Another writer changed the project.\nMAVEN · widget: not saved.';
      } else if (prompt.includes('Turn off MAVEN')) {
        response = 'MAVEN is off for this conversation.';
      } else if (prompt.includes('two plus two') && context.messages.some(m => txt(m).includes('Turn off MAVEN'))) {
        response = 'Four.';
      }
      content ??= [{type:'text', text:response}];
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
