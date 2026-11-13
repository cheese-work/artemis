// Combines whats-new/entries/*.json (one file per user-facing PR) into
// public/whats-new.json, newest first. Runs as `prebuild`, so a malformed
// entry fails the build. Usage: node validate-whats-new.mjs [entriesDir] [outFile]
import { readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appDirectory = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const entriesDirectory = process.argv[2] ?? path.join(appDirectory, 'whats-new/entries');
const outFile = process.argv[3] ?? path.join(appDirectory, 'public/whats-new.json');
const allowedKeys = new Set(['id', 'date', 'title', 'body', 'issues']);

function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

function problem(entry, id) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return 'must be one JSON object';
  const unknown = Object.keys(entry).filter(key => !allowedKeys.has(key));
  if (unknown.length) return `has unknown keys: ${unknown.join(', ')}`;
  if (entry.id !== id) return `id must equal the file name without .json ("${id}")`;
  if (!validDate(entry.date)) return 'date must be a real YYYY-MM-DD date';
  if (!id.startsWith(`${entry.date}-`)) return 'file name must start with the entry date: <YYYY-MM-DD>-<slug>.json';
  if (typeof entry.title !== 'string' || !entry.title.trim()) return 'title must be a non-empty string';
  if (entry.body !== undefined && typeof entry.body !== 'string') return 'body must be a string';
  if (entry.issues !== undefined
    && (!Array.isArray(entry.issues) || !entry.issues.every(issue => typeof issue === 'string' && /^CHE-\d+$/.test(issue)))) {
    return 'issues must be an array of issue numbers like "CHE-1334"';
  }
  return null;
}

const files = (await readdir(entriesDirectory)).filter(name => name.endsWith('.json')).sort();
const entries = [];
for (const file of files) {
  const id = file.slice(0, -'.json'.length);
  let entry;
  try {
    entry = JSON.parse(await readFile(path.join(entriesDirectory, file), 'utf8'));
  } catch (error) {
    throw new Error(`What's New entry ${file} is not valid JSON: ${error.message}`);
  }
  const reason = problem(entry, id);
  if (reason) throw new Error(`What's New entry ${file} ${reason}`);
  entries.push(entry);
}
// File names are unique, so ids are too; ties on date sort by id.
entries.sort((a, b) => b.date.localeCompare(a.date) || a.id.localeCompare(b.id));

await writeFile(outFile, `${JSON.stringify(entries, null, 2)}\n`);
console.log(`Combined ${entries.length} What's New entr${entries.length === 1 ? 'y' : 'ies'} into ${path.relative(process.cwd(), outFile)}.`);
