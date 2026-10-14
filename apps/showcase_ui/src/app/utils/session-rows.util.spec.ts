import { sessionRows } from './session-rows.util';
import { RunSummary } from '../core/models/run.model';

describe('Sessions rows', () => {
  it('groups turns across cursor pages, keeping the newest status and first prompt', () => {
    const latest = { session_id: 'turn-2', chat_session_id: 'root', chat_title: 'Login checks', chat_prompt: 'First prompt', prompt: 'Follow up', status: 'running' } as RunSummary;
    const first = { session_id: 'root', chat_session_id: 'root', prompt: 'First prompt', status: 'completed' } as RunSummary;
    const single = { session_id: 'legacy', prompt: 'Legacy', status: 'failed' } as RunSummary;
    const rows = sessionRows([latest, first, single]);
    expect(rows.length).toBe(2);
    expect(rows[0].session_id).toBe('turn-2');
    expect(rows[0].prompt).toBe('First prompt');
    expect(rows[0].chat_title).toBe('Login checks');
    expect(rows[0].status).toBe('running');
    expect(rows[1]).toEqual(single);
  });
});
