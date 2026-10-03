import { cp, lstat, mkdir, mkdtemp, readFile, readdir, rm, symlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appDirectory = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repositoryDirectory = path.resolve(appDirectory, '../..');
const dockerfile = await readFile(path.join(repositoryDirectory, 'Dockerfile'), 'utf8');
const stages = dockerfile.split(/^FROM\s+/m);
const frontendStage = stages.find((stage) => stage.startsWith('node:') && stage.includes('AS frontend-builder'));
if (!frontendStage) throw new Error('Dockerfile frontend-builder stage was not found');

const copyInstructions = frontendStage.split('\n')
  .map((line) => line.trim())
  .filter((line) => line.startsWith('COPY '))
  .map((line) => line.slice('COPY '.length).split(/\s+/));
const buildInstruction = frontendStage.split('\n')
  .map((line) => line.trim())
  .find((line) => line.startsWith('RUN npm run build'));
if (!buildInstruction || copyInstructions.length === 0) {
  throw new Error('Dockerfile frontend-builder stage must copy inputs and run npm run build');
}

await mkdir(path.join(appDirectory, '.angular'), { recursive: true });
const stageDirectory = await mkdtemp(path.join(appDirectory, '.angular/docker-build-inputs-'));

async function expandSource(source) {
  if (!/[*?]/.test(source)) return [source];
  const directory = path.dirname(source);
  const basename = path.basename(source);
  const expression = new RegExp(`^${basename.split('').map((character) => {
    if (character === '*') return '.*';
    if (character === '?') return '.';
    return character.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('')}$`);
  return (await readdir(path.join(repositoryDirectory, directory)))
    .filter((name) => expression.test(name))
    .map((name) => path.join(directory, name));
}

try {
  await mkdir(stageDirectory, { recursive: true });
  for (const instruction of copyInstructions) {
    const destination = instruction.at(-1);
    const sourcePatterns = instruction.slice(0, -1);
    for (const sourcePattern of sourcePatterns) {
      const sources = await expandSource(sourcePattern);
      for (const source of sources) {
        const sourcePath = path.join(repositoryDirectory, source);
        const sourceIsDirectory = (await lstat(sourcePath)).isDirectory();
        const destinationPath = path.resolve(stageDirectory, destination);
        const targetPath = sourceIsDirectory
          ? destinationPath
          : destination.endsWith('/') || sourcePatterns.length > 1
          ? path.join(destinationPath, path.basename(source.replace(/\/$/, '')))
          : destinationPath;
        await mkdir(path.dirname(targetPath), { recursive: true });
        await cp(sourcePath, targetPath, { recursive: true });
      }
    }
  }
  await symlink(path.join(appDirectory, 'node_modules'), path.join(stageDirectory, 'node_modules'), 'dir');
  execFileSync('npm', ['run', 'build'], { cwd: stageDirectory, stdio: 'inherit' });
  console.log('Docker frontend copied-input build passed.');
} finally {
  await rm(stageDirectory, { recursive: true, force: true });
}
