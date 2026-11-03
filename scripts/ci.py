#!/usr/bin/env python3
"""Build source-pinned native libraries, embed them, and publish verified artifacts."""
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'build-config.json'
NATIVE_FILES = {'linux-amd64': 'libysm.so', 'macos-arm64': 'libysm.dylib', 'macos-amd64': 'libysm-x86_64.dylib'}


def run(*args, cwd=ROOT, env=None, capture=False):
    print('+', ' '.join(map(str, args)), flush=True)
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env,
                            text=True, check=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def download(url, expected, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)
    if digest(destination) != expected:
        raise RuntimeError(f'Checksum mismatch: {destination.name}')
    return destination


def checkout_sha(path):
    return run('git', 'rev-parse', 'HEAD', cwd=path, capture=True)


def validate_sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Source revisions must be full, lowercase Git commit SHAs')
    return value


def release_tag(java, native, ci):
    return f'dev-{validate_sha(java)[:12]}-{validate_sha(native)[:12]}-{validate_sha(ci)[:12]}'


def resolve():
    config = json.loads(CONFIG.read_text())
    event = os.environ.get('GITHUB_EVENT_NAME', 'workflow_dispatch')
    latest = event == 'schedule' or os.environ.get('SOURCE_MODE') == 'latest'
    refs = {}
    for key in ('java', 'native'):
        spec = config[key]
        if latest:
            lines = run('git', 'ls-remote', f'https://github.com/{spec["repository"]}.git',
                        f'refs/heads/{spec["branch"]}', capture=True).splitlines()
            if len(lines) != 1:
                raise RuntimeError(f'Cannot resolve {key} branch')
            refs[key] = validate_sha(lines[0].split()[0])
        else:
            refs[key] = validate_sha(spec['commit'])
    ci = validate_sha(os.environ['GITHUB_SHA'])
    tag = release_tag(refs['java'], refs['native'], ci)
    ref = os.environ.get('GITHUB_REF', '')
    tagged = event == 'push' and ref.startswith('refs/tags/v')
    if tagged:
        tag = ref.removeprefix('refs/tags/')
        if not re.fullmatch(r'v[A-Za-z0-9._+-]+', tag):
            raise ValueError('Release tags must match v[A-Za-z0-9._+-]+')
    publish = event == 'schedule' or tagged or (
        event == 'workflow_dispatch' and os.environ.get('PUBLISH') == 'true')
    should_build = True
    if publish:
        url = (f'https://api.github.com/repos/{os.environ["GITHUB_REPOSITORY"]}'
               f'/releases/tags/{urllib.parse.quote(tag, safe="")}')
        request = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {os.environ["GH_TOKEN"]}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                existing = json.load(response)
            should_build = bool(existing.get('draft'))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    values = {**refs, 'mdspan': validate_sha(config['mdspan_commit']), 'tag': tag,
              'build': str(should_build).lower(), 'publish': str(publish).lower()}
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')
    print(json.dumps(values, indent=2))


def assert_sources():
    for key in ('java', 'native'):
        path = ROOT / 'sources' / key
        expected = os.environ.get(f'EXPECTED_{key.upper()}_SHA')
        if expected and path.exists() and checkout_sha(path) != validate_sha(expected):
            raise RuntimeError(f'Wrong {key} source checkout')


def native():
    config = json.loads(CONFIG.read_text())
    target = os.environ['TARGET']
    expected_host = {'linux-amd64': ('Linux', 'x86_64'), 'macos-arm64': ('Darwin', 'arm64'), 'macos-amd64': ('Darwin', 'x86_64')}
    if (platform.system(), platform.machine()) != expected_host[target]:
        raise RuntimeError(f'Runner architecture does not match {target}')
    assert_sources()
    if checkout_sha(ROOT / 'sources/mdspan') != validate_sha(config['mdspan_commit']):
        raise RuntimeError('Wrong mdspan source checkout')
    version = run('git', '--version', capture=True).split()[-1].split('.')
    if tuple(map(int, version[:2])) < (2, 49):
        raise RuntimeError('Git >= 2.49 is required for dependency clones with --revision')
    tools = ROOT / '.tools'
    tools.mkdir(exist_ok=True)
    nasm_archive = download(config['nasm_url'], config['nasm_sha256'], tools / 'nasm.tar.xz')
    with tarfile.open(nasm_archive) as archive:
        archive.extractall(tools, filter='data')
    run('./configure', f'--prefix={tools}', cwd=tools / 'nasm-3.01')
    run('make', '-j2', cwd=tools / 'nasm-3.01')
    run('make', 'install', cwd=tools / 'nasm-3.01')
    env = dict(os.environ)
    env['PATH'] = f'{tools / "bin"}{os.pathsep}{env["PATH"]}'
    if target == 'linux-amd64':
        archive = download(config['clang_url'], config['clang_sha256'], tools / 'clang.7z')
        run('7z', 'x', '-y', f'-o{tools}', archive)
        clang = tools / 'clang-23.1.4-x86_64'
        for executable in (clang / 'bin').iterdir():
            if executable.is_file():
                executable.chmod(executable.stat().st_mode | 0o111)
        compiler = clang / 'bin/clang++'
        c_compiler = clang / 'bin/clang'
        env['LD_LIBRARY_PATH'] = str(clang / 'lib')
        env['PATH'] = f'{clang / "bin"}{os.pathsep}{env["PATH"]}'
        env['CC'], env['CXX'] = 'gcc-13', 'g++-13'
        dependency_compiler = run('g++-13', '--version', capture=True).splitlines()[0]
    else:
        compiler = Path(run('xcrun', '--find', 'clang++', capture=True))
        c_compiler = Path(run('xcrun', '--find', 'clang', capture=True))
        env['CC'], env['CXX'] = str(c_compiler), str(compiler)
        env['MACOSX_DEPLOYMENT_TARGET'] = config['macos_deployment_target']
        dependency_compiler = run(compiler, '--version', capture=True).splitlines()[0]
    probe = subprocess.run([str(compiler), '-std=c++23', '-x', 'c++', '-fsyntax-only', '-'],
        input='#include <mdspan>\nusing T = std::mdspan<int, std::extents<int, 2, 2>>;\n',
        text=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    supplemented = probe.returncode != 0
    if supplemented:
        include = tools / 'cxx-include'
        include.mkdir(exist_ok=True)
        code = run(sys.executable, ROOT / 'sources/mdspan/make_single_header.py',
                   ROOT / 'sources/mdspan/include/mdspan/mdspan.hpp', capture=True)
        (include / 'mdspan').write_text('#include <span>\n#define MDSPAN_IMPL_STANDARD_NAMESPACE std\n' + code)
        env['CPLUS_INCLUDE_PATH'] = str(include)
    source = ROOT / 'sources/native'
    env['CONAN_HOME'] = str(source / 'conan/cache')
    if target.startswith('macos-'):
        mac_target = source / 'conan/cmake/targets/macos-arm64.cmake'
        template = mac_target.read_text()
        if template.count('-funique-source-file-names') != 1:
            raise RuntimeError('macOS target template changed; review Apple Clang options')
        template = template.replace('        -funique-source-file-names\n', '')
        if os.environ.get('YSM_DIAGNOSTIC_SYMBOLS') == '1':
            original = '-Wl,-S,-x,-undefined,error,-dead_strip'
            if template.count(original) != 1:
                raise RuntimeError('macOS linker flags changed; review diagnostic symbols')
            template = template.replace(original, '-Wl,-undefined,error,-dead_strip')
        mac_target.write_text(template)
    if target == 'macos-amd64':
        if template.count('-mcpu=apple-m1') != 1:
            raise RuntimeError('macOS target template changed; review Intel settings')
        (source / 'conan/cmake/targets/macos-x64.cmake').write_text(template.replace('-mcpu=apple-m1', '-march=x86-64'))
    run('bash', 'bootstrap.sh', 'setup', cwd=source, env=env)
    run('conan', 'profile', 'detect', '--force', cwd=source, env=env)
    run('conan', 'install', '.', '-pr:h', 'default', '-pr:h', 'conan/profile-override',
        '-pr:b', 'default', '--no-remote', '--build=missing', '-s', 'build_type=Release',
        '-c:h', 'user.target:profile=native', '-c', 'tools.build:jobs=3',
        '-c', "tools.cmake.cmaketoolchain:extra_variables={'CMAKE_CXX_SCAN_FOR_MODULES': False}",
        '-c', "svt-av1/*:tools.cmake.cmaketoolchain:extra_variables={'SVT_AV1_LTO': False, 'CMAKE_CXX_SCAN_FOR_MODULES': False}",
        '--core-conf', 'core.graph:compatibility_mode=optimized', cwd=source, env=env)
    build = source / 'build/ci-release'
    args = ['cmake', '-S', str(source), '-B', str(build), '-G', 'Ninja',
            f'-DCMAKE_TOOLCHAIN_FILE={source}/build/native-release/generators/conan_toolchain.cmake',
            f'-DCMAKE_CXX_COMPILER={compiler}', f'-DCMAKE_C_COMPILER={c_compiler}',
            '-DCMAKE_BUILD_TYPE=Release', '-DYSM_DEPENDENCY_PROVIDER=conan',
            '-DCMAKE_CXX_SCAN_FOR_MODULES=OFF']
    if target == 'linux-amd64':
        args.append('-DCMAKE_LINKER_TYPE=LLD')
    if target.startswith('macos-') and os.environ.get('YSM_DIAGNOSTIC_SYMBOLS') == '1':
        args.append('-DCMAKE_CXX_FLAGS_RELEASE=-O3 -DNDEBUG -g')
    run(*args, env=env)
    run('cmake', '--build', build, '--target', 'ysm-lib-jni', '-j3', env=env)
    library = build / 'modules/lib-jni' / ('libysm.dylib' if target.startswith('macos-') else NATIVE_FILES[target])
    if target.startswith('macos-'):
        run('codesign', '--force', '--sign', '-', library)
    inspect_library(library, target)
    # JNI initialization is checked by the JVM smoke job on each target.
    destination = ROOT / 'artifacts' / target
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(library, destination / NATIVE_FILES[target])
    for filename in ('LICENSE', 'THIRD_PARTY_LICENSES.md'):
        shutil.copy2(source / filename, destination / filename)
    if supplemented:
        shutil.copy2(ROOT / 'sources/mdspan/LICENSE', destination / 'mdspan-LICENSE')
    java_abi = re.search(r'NATIVE = "([^"]+)"',
        (ROOT / 'sources/java/src/declaration/java/com/elfmcys/ysm/compatibility/AbiVersion.java').read_text())[1]
    native_abi = re.search(r'kNativeAbi = "([^"]+)"',
        (source / 'modules/lib-jni/src/runtime/abi_version.h').read_text())[1]
    if java_abi != native_abi:
        raise RuntimeError('Java/native ABI mismatch')
    metadata = {'target': target, 'file': NATIVE_FILES[target], 'sha256': digest(library),
        'java_commit': checkout_sha(ROOT / 'sources/java'), 'native_commit': checkout_sha(source),
        'abi': native_abi, 'compiler': run(compiler, '--version', capture=True, env=env).splitlines()[0],
        'dependency_compiler': dependency_compiler, 'svt_av1_lto': False,
        'mdspan_supplement': supplemented, 'mdspan_commit': config['mdspan_commit']}
    if target == 'linux-amd64':
        versions = re.findall(r'GLIBC_(\d+(?:\.\d+)+)', run('readelf', '--version-info', library, capture=True))
        metadata['minimum_glibc'] = max(versions, key=lambda v: tuple(map(int, v.split('.'))))
    else:
        metadata['deployment_target'] = config['macos_deployment_target']
    (destination / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')


def inspect_library(path, target):
    data = Path(path).read_bytes()
    if target == 'linux-amd64':
        if data[:5] != b'\x7fELF\x02' or struct.unpack_from('<H', data, 18)[0] != 62:
            raise RuntimeError('Expected Linux ELF amd64 library')
        run('readelf', '-d', path)
    elif data[:4] != bytes.fromhex('cffaedfe') or struct.unpack_from('<I', data, 4)[0] != {'macos-arm64': 0x100000C, 'macos-amd64': 0x1000007}[target]:
        raise RuntimeError('Unexpected macOS Mach-O architecture')
    else:
        output = run('otool', '-L', path, capture=True)
        print(output)
        # All third-party dependencies must be static; only Apple system libraries are allowed.
        for line in output.splitlines()[2:]:
            name = line.strip().split(' (')[0]
            if not name.startswith(('/usr/lib/', '/System/Library/')):
                raise RuntimeError(f'Non-system macOS dynamic dependency: {name}')


def platform_metadata(directory):
    """Accept exactly two native artifacts and require one coherent source/ABI pair."""
    result = {}
    for target, filename in NATIVE_FILES.items():
        folder = Path(directory) / f'native-{target}'
        meta = json.loads((folder / 'metadata.json').read_text())
        if meta['target'] != target or meta['file'] != filename or digest(folder / filename) != meta['sha256']:
            raise RuntimeError(f'Invalid native artifact: {target}')
        for key in ('java', 'native'):
            if meta[f'{key}_commit'] != os.environ[f'EXPECTED_{key.upper()}_SHA']:
                raise RuntimeError(f'Mixed {key} source revisions in artifacts')
        result[target] = meta
    if len({m['abi'] for m in result.values()}) != 1:
        raise RuntimeError('Native artifacts have different ABIs')
    return result


def package():
    config = json.loads(CONFIG.read_text())
    assert_sources()
    java = ROOT / 'sources/java'
    metadata = platform_metadata(ROOT / 'artifacts')
    resources = java / 'src/main/resources/META-INF'
    (resources / 'native').mkdir(parents=True, exist_ok=True)
    run('git', 'apply', '--check', ROOT / 'patches/macos-loader.patch', cwd=java)
    run('git', 'apply', ROOT / 'patches/macos-loader.patch', cwd=java)
    for target, filename in NATIVE_FILES.items():
        shutil.copy2(ROOT / 'artifacts' / f'native-{target}' / filename, resources / 'native' / filename)
        notices = resources / 'community-licenses' / target
        notices.mkdir(parents=True, exist_ok=True)
        for notice in (ROOT / 'artifacts' / f'native-{target}').glob('*LICENSE*'):
            shutil.copy2(notice, notices / notice.name)
    provenance = {'community_build': True, 'platforms': metadata,
                  'ci_commit': os.environ.get('GITHUB_SHA'), 'loader_patch_sha256': digest(ROOT / 'patches/macos-loader.patch')}
    (resources / 'ysm-community-build.json').write_text(json.dumps(provenance, indent=2) + '\n')
    tools = ROOT / '.tools'
    archive = download(config['gradle_url'], config['gradle_sha256'], tools / 'gradle.zip')
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(tools)
    gradle = tools / 'gradle-8.12/bin/gradle'
    gradle.chmod(0o755)
    env = dict(os.environ)
    env['YSM_NATIVE_PATH'] = str(resources / 'native/libysm.so')
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    env['CI_SMOKE_DIR'] = str(dist / 'smoke')
    paths = [env['JAVA_HOME_17_X64'], env['JAVA_HOME_21_X64']]
    # Generate Minecraft mappings before resolving the verification classpath.
    run(gradle, '--no-daemon', '--console=plain',
        '--init-script', ROOT / 'scripts/repositories.init.gradle',
        '--init-script', ROOT / 'scripts/smoke.init.gradle',
        f'-Porg.gradle.java.installations.paths={",".join(paths)}',
        'createMinecraftArtifacts', cwd=java, env=env)
    run(gradle, '--no-daemon', '--console=plain',
        '--init-script', ROOT / 'scripts/repositories.init.gradle',
        '--init-script', ROOT / 'scripts/smoke.init.gradle',
        f'-Porg.gradle.java.installations.paths={",".join(paths)}',
        'shadowJar', 'verifyBuiltinModelIndex', 'verifyPackagedSoundLinkage', 'ciSmokeDependencies',
        cwd=java, env=env)
    jars = [p for p in (java / 'build/libs').glob('*.jar') if not any(
        word in p.name for word in ('sources', 'javadoc', 'declaration-processor'))]
    if len(jars) != 1:
        raise RuntimeError(f'Expected one production mod JAR, got {jars}')
    with zipfile.ZipFile(jars[0]) as zipped:
        if zipped.testzip() is not None:
            raise RuntimeError('JAR integrity check failed')
        for target, filename in NATIVE_FILES.items():
            if hashlib.sha256(zipped.read(f'META-INF/native/{filename}')).hexdigest() != metadata[target]['sha256']:
                raise RuntimeError(f'Embedded {target} library mismatch')
        if 'com/elfmcys/ysm/util/BundledMacLibrary.class' not in zipped.namelist():
            raise RuntimeError('macOS runtime loader patch is absent')
    shutil.copy2(jars[0], dist / jars[0].name)
    (dist / 'build-info.json').write_text(json.dumps(provenance, indent=2) + '\n')
    smoke_classpath = os.pathsep.join([str(jars[0]), *map(str, (dist / 'smoke').glob('*.jar'))])
    run(Path(paths[0]) / 'bin/javac', '-cp', smoke_classpath, '-d', dist / 'smoke', ROOT / 'scripts/NativeSmoke.java')
    (dist / 'release-notes.md').write_text(
        '# Community development build\n\n'
        'One Forge 1.20.1 JAR with Linux amd64 and macOS Apple Silicon and Intel libraries.\n\n'
        f'Java source: `{os.environ["EXPECTED_JAVA_SHA"]}`\n\n'
        f'Native source: `{os.environ["EXPECTED_NATIVE_SHA"]}`\n\n'
        f'Linux requires glibc {metadata["linux-amd64"]["minimum_glibc"]} or newer. '
        f'macOS deployment target: {config["macos_deployment_target"]}. Windows and Android are not built here.\n\n'
        'Build changes: explicit macOS runtime-loader patch; Linux Clang/GCC toolchain; '
        'Kokkos mdspan header supplement where unavailable; SVT-AV1 LTO disabled. '
        'macOS uses Xcode Apple Clang and the system libc++ runtime. '
        'Checks: built-in model materialization, packaged sound linkage, per-platform JNI initialization. '
        'No full in-game session is run. Not an official YSM release.\n')
    with zipfile.ZipFile(dist / 'ysm-linux-macos.zip', 'w', zipfile.ZIP_DEFLATED) as zipped:
        zipped.write(dist / jars[0].name, jars[0].name)
        for target, filename in NATIVE_FILES.items():
            zipped.write(ROOT / 'artifacts' / f'native-{target}' / filename, f'native/{target}/{filename}')
        for filename in ('build-info.json', 'release-notes.md'):
            zipped.write(dist / filename, filename)
        zipped.write(ROOT / 'patches/macos-loader.patch', 'patches/macos-loader.patch')
        zipped.write(java / 'LICENSE', 'licenses/Java-LICENSE')
        zipped.write(java / 'NOTICE.md', 'licenses/Java-NOTICE.md')
        for target in NATIVE_FILES:
            folder = ROOT / 'artifacts' / f'native-{target}'
            for notice in folder.glob('*LICENSE*'):
                zipped.write(notice, f'licenses/{target}-{notice.name}')
    files = [*dist.glob('*.jar'), *dist.glob('*.zip')]
    (dist / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in sorted(files)))


def smoke():
    dist = ROOT / 'dist'
    target = os.environ['TARGET']
    jar, = dist.glob('*.jar')
    with zipfile.ZipFile(jar) as zipped:
        meta = json.loads(zipped.read('META-INF/ysm-community-build.json'))['platforms'][target]
        data = zipped.read(f'META-INF/native/{NATIVE_FILES[target]}')
    if hashlib.sha256(data).hexdigest() != meta['sha256']:
        raise RuntimeError('Final JAR native resource was corrupted')
    path = (dist / NATIVE_FILES[target]).resolve()
    path.write_bytes(data)
    inspect_library(path, target)
    classpath = os.pathsep.join([str(dist / 'smoke'), str(jar), *map(str, (dist / 'smoke').glob('*.jar'))])
    run('java', '-cp', classpath, 'NativeSmoke', path, target)


if __name__ == '__main__':
    commands = {'resolve': resolve, 'native': native, 'package': package, 'smoke': smoke}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit('Usage: ci.py resolve|native|package|smoke')
    commands[sys.argv[1]]()
