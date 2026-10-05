import { classifySearch } from './run-search.util';

describe('classifySearch', () => {
  const uuid = '3f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';

  it('treats blank input as empty', () => {
    expect(classifySearch('')).toEqual({ kind: 'empty' });
    expect(classifySearch('   ')).toEqual({ kind: 'empty' });
  });

  it('jumps on a full uuid or an 8-character id, lowercased and trimmed', () => {
    expect(classifySearch(uuid)).toEqual({ kind: 'jump', id: uuid });
    expect(classifySearch(`  ${uuid.toUpperCase()} `)).toEqual({ kind: 'jump', id: uuid });
    expect(classifySearch('3f2b9c1a')).toEqual({ kind: 'jump', id: '3f2b9c1a' });
    expect(classifySearch(' 3F2B9C1A ')).toEqual({ kind: 'jump', id: '3f2b9c1a' });
  });

  it('treats everything else as text and passes it through untouched', () => {
    expect(classifySearch('login')).toEqual({ kind: 'text', q: 'login' });
    expect(classifySearch('  login flow ')).toEqual({ kind: 'text', q: 'login flow' });
    expect(classifySearch('3f2b9c1')).toEqual({ kind: 'text', q: '3f2b9c1' });
    expect(classifySearch('3f2b9c1ab')).toEqual({ kind: 'text', q: '3f2b9c1ab' });
    expect(classifySearch('zzzzzzzz')).toEqual({ kind: 'text', q: 'zzzzzzzz' });
    expect(classifySearch(`${uuid} extra`)).toEqual({ kind: 'text', q: `${uuid} extra` });
  });

  it('never builds search syntax on the client: the server quotes the text', () => {
    expect(classifySearch('a OR "b NEAR(c')).toEqual({ kind: 'text', q: 'a OR "b NEAR(c' });
  });
});
