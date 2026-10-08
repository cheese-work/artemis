import { readFile } from 'node:fs/promises';

const file = new URL('../public/whats-new.json', import.meta.url);
const entries = JSON.parse(await readFile(file, 'utf8'));

function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

if (!Array.isArray(entries)) throw new Error('whats-new.json must be an array');

const ids = new Set();
let previousDate = null;
for (const [index, entry] of entries.entries()) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)
    || typeof entry.id !== 'string' || !entry.id.trim()
    || !validDate(entry.date)
    || typeof entry.title !== 'string' || !entry.title.trim()
    || (entry.body !== undefined && typeof entry.body !== 'string')) {
    throw new Error(`whats-new.json entry ${index} does not match the {id, date, title, body?} schema`);
  }
  if (ids.has(entry.id)) throw new Error(`whats-new.json contains duplicate id: ${entry.id}`);
  if (previousDate !== null && entry.date > previousDate) {
    throw new Error('whats-new.json entries must be ordered newest-first by date');
  }
  ids.add(entry.id);
  previousDate = entry.date;
}

console.log(`Validated ${entries.length} newest-first What's New entr${entries.length === 1 ? 'y' : 'ies'}.`);
