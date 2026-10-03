// Private macOS arm64 build. Downloads toolchain artifacts, never account data.
import { execFileSync } from 'node:child_process';
import { cp, lstat, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { packager } from '@electron/packager';
import { APP_FILES, PYTHON, auditPayload, copyAppFiles, ensureBuildDirectory, inventoryTree, readJSON, sha256, writeJSON } from './packaging-utils.mjs';

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..'), repo = dirname(desktop);
const build = join(desktop, 'build'), output = join(desktop, 'dist');
const python = join(build, 'python'), stage = join(build, 'app'), licenses = join(build, 'licenses');
const run = (program, args, cwd = repo) => execFileSync(program, args, { cwd, stdio: 'inherit' });
const capture = (program, args, cwd = repo) => execFileSync(program, args, { cwd, encoding: 'utf8' }).trim();
if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('This private build targets macOS arm64 only.');
await ensureBuildDirectory(desktop, 'build'); await ensureBuildDirectory(desktop, 'dist');
for (const folder of [python, stage, licenses, join(build, 'python-extracted'), join(build, 'wheels')]) {
  await ensureBuildDirectory(build, folder.split('/').at(-1));
  await rm(folder, { recursive: true, force: true });
  await mkdir(folder, { recursive: true });
}
run('npm', ['run', 'build'], desktop);
const downloads = await ensureBuildDirectory(build, 'downloads');
const archive = join(downloads, `python-${PYTHON.version}-full.tar.zst`);
let bytes;
try {
  if (!(await lstat(archive)).isFile()) throw new Error('Python archive must be a regular file.');
  bytes = await readFile(archive);
} catch (error) { if (error.code !== 'ENOENT') throw error; }
if (!bytes || sha256(bytes) !== PYTHON.sha256) {
  console.log(`Downloading pinned CPython ${PYTHON.version} and its original license inventory.`);
  const response = await fetch(PYTHON.url);
  if (!response.ok) throw new Error(`Python download failed: ${response.status}`);
  bytes = Buffer.from(await response.arrayBuffer());
  if (sha256(bytes) !== PYTHON.sha256) throw new Error('Python archive checksum mismatch.');
  await writeFile(archive, bytes);
}
const extracted = join(build, 'python-extracted');
run('/usr/bin/tar', ['-xf', archive, '-C', extracted, 'python/install', 'python/licenses', 'python/PYTHON.json']);
await cp(join(extracted, 'python/install'), python, { recursive: true, verbatimSymlinks: true });
await cp(join(extracted, 'python/licenses'), join(licenses, 'python'), { recursive: true });
await cp(join(extracted, 'python/PYTHON.json'), join(licenses, 'python/PYTHON.json'));
// Remove packaging tools and scripts with installation-specific shebangs. The
// installed CPython, standard library and native dependencies remain intact.
for (const entry of await readdir(join(python, 'bin'))) {
  if (entry !== 'python3.13') await rm(join(python, 'bin', entry), { recursive: true, force: true });
}
for (const entry of ['include', 'share', 'lib/pkgconfig', 'lib/python3.13/test']) {
  await rm(join(python, entry), { recursive: true, force: true });
}
const site = join(python, 'lib/python3.13/site-packages');
await rm(site, { recursive: true, force: true }); await mkdir(site, { recursive: true });
const interpreter = join(python, 'bin/python3.13'), requirements = join(build, 'runtime-requirements.txt');
capture('uv', ['export', '--locked', '--no-dev', '--no-emit-project', '--no-header', '--no-annotate', '--output-file', requirements]);
run('uv', ['pip', 'install', '--python', interpreter, '--target', site, '--require-hashes', '--only-binary', ':all:', '-r', requirements]);
run('uv', ['build', '--wheel', '--out-dir', join(build, 'wheels')]);
const wheels = (await readdir(join(build, 'wheels'))).filter(name => name.endsWith('.whl'));
if (wheels.length !== 1) throw new Error('Expected one GCT wheel.');
run('uv', ['pip', 'install', '--python', interpreter, '--target', site, '--no-deps', join(build, 'wheels', wheels[0])]);
// direct_url is install provenance only; it would leak this build's checkout
// path. The shipped build manifest records the wheel hash instead.
for (const entry of await readdir(site)) {
  if (entry.endsWith('.dist-info')) await rm(join(site, entry, 'direct_url.json'), { force: true });
}
for (const file of await inventoryTree(python)) {
  if (/\.(?:pyc|a|o)$/.test(file.path)) await rm(join(python, file.path));
}
const pythonPackages = JSON.parse(capture(interpreter, ['-I', '-B', '-c',
  'import importlib.metadata as m,json; print(json.dumps(sorted([{ "name":d.metadata["Name"],"version":d.version} for d in m.distributions()],key=lambda d:d["name"])))']));
await copyAppFiles(desktop, stage);
const manifest = await readJSON(join(desktop, 'package.json'));
await writeJSON(join(stage, 'package.json'), { name: manifest.name, version: manifest.version,
  private: true, type: 'module', main: 'main.mjs' });
const packagePaths = capture('npm', ['ls', '--omit=dev', '--all', '--parseable'], desktop).split('\n').slice(1);
const nodePackages = [];
for (const packagePath of packagePaths) {
  const metadata = await readJSON(join(packagePath, 'package.json'));
  const target = join(stage, 'node_modules', metadata.name);
  // This lock currently uses a flat production graph; fail rather than silently
  // flatten distinct versions if the dependency graph changes in the future.
  if (nodePackages.some(item => item.name === metadata.name)) throw new Error('Nested production dependencies need explicit packaging support.');
  nodePackages.push({ name: metadata.name, version: metadata.version });
  await mkdir(target, { recursive: true });
  if (metadata.name === '@siwc/local') {
    for (const name of ['package.json', 'dist', 'src', 'LICENSE', 'THIRD_PARTY_NOTICES.md']) {
      await cp(join(packagePath, name), join(target, name), { recursive: true, dereference: true });
    }
    await cp(join(desktop, 'vendor/README.md'), join(target, 'GCT_MODIFICATIONS.md'));
  } else await cp(packagePath, target, { recursive: true, dereference: true,
    filter: path => !/(?:^|\/)(?:test|tests|\.github|\.git|node_modules)(?:\/|$)/.test(path.slice(packagePath.length)) });
}
await cp(join(desktop, 'node_modules/electron/dist/LICENSE'), join(licenses, 'Electron-LICENSE.txt'));
await cp(join(desktop, 'node_modules/electron/dist/LICENSES.chromium.html'), join(licenses, 'Electron-Chromium-LICENSES.html'));
await writeFile(join(licenses, 'README.txt'), `This is a personal, noncommercial GCT preview, with no Developer ID signature or notarization.\n\nOpenAI SDK license and notices: app.asar/node_modules/@siwc/local/{LICENSE,THIRD_PARTY_NOTICES.md,GCT_MODIFICATIONS.md}. Modified source and compiled comments are preserved.\nJavaScript dependency licenses are retained in each app.asar/node_modules package.\nPython ${PYTHON.version} (${PYTHON.release}) and native-library license texts plus the exact upstream PYTHON.json are in licenses/python. Installed Python dependency licenses are retained under python/lib/python3.13/site-packages/*.dist-info.\nElectron/Chromium license texts are alongside this file.\nThe SDK license does not grant service access or commercial distribution rights. Each user must connect their own eligible ChatGPT account; GCT has no paid API fallback.\n`);
const electronVersion = (await readJSON(join(desktop, 'node_modules/electron/package.json'))).version;
const wheelHash = sha256(await readFile(join(build, 'wheels', wheels[0])));
await writeJSON(join(licenses, 'build-manifest.json'), { version: manifest.version, platform: 'darwin', arch: 'arm64',
  electronVersion, python: PYTHON, pythonPackages, nodePackages,
  gctWheelSha256: wheelHash, uvLockSha256: sha256(await readFile(join(repo, 'uv.lock'))),
  npmLockSha256: sha256(await readFile(join(desktop, 'package-lock.json'))),
  applicationInputs: APP_FILES, signing: 'ad-hoc local only; not Developer ID or notarized',
  sdkUpstreamCommit: 'f723814abdccec135b519c451fb6e1992ee5e933' });
await auditPayload(stage); await auditPayload(python);
await ensureBuildDirectory(output, 'Grounded Class Tutor-darwin-arm64');
const [bundleDirectory] = await packager({ dir: stage, out: output, name: 'Grounded Class Tutor',
  appBundleId: 'com.natenc12.grounded-class-tutor', appVersion: manifest.version, electronVersion,
  platform: 'darwin', arch: 'arm64', asar: true, prune: false, overwrite: true,
  extraResource: [python, licenses], appCategoryType: 'public.app-category.education',
  darwinDarkModeSupport: true, quiet: true });
const app = join(bundleDirectory, 'Grounded Class Tutor.app');
// Preserve local execution, without implying a trusted Developer ID release.
run('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', app]);
run('/usr/bin/codesign', ['--verify', '--deep', '--strict', app]);
await auditPayload(app);
run(process.execPath, [join(desktop, 'scripts/smoke-packaged.mjs'), app], desktop);
const zip = join(output, `Grounded-Class-Tutor-${manifest.version}-mac-arm64.zip`);
await rm(zip, { force: true });
run('/usr/bin/ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', app, zip]);
await writeFile(`${zip}.sha256`, `${sha256(await readFile(zip))}  ${zip.split('/').at(-1)}\n`);
console.log(`Private app: ${app}\nPrivate archive: ${zip}`);
