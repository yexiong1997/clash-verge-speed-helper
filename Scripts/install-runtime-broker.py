#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time


DEFAULT_ROOT = Path.home() / 'Library/Application Support/ClashVergeSpeedHelper/Runtime'
DEFAULT_CONFIG = Path.home() / 'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/verge.yaml'
RUBY_FIELD = r'''
require 'psych'
require 'json'
tree = Psych.parse_stream(STDIN.read)
raise 'expected one YAML document' unless tree.children.size == 1
mapping = tree.children[0].root
raise 'expected block YAML mapping' unless mapping.is_a?(Psych::Nodes::Mapping) && mapping.style == Psych::Nodes::Mapping::BLOCK
matches = mapping.children.each_slice(2).select { |key, _| key.is_a?(Psych::Nodes::Scalar) && key.value == 'startup_script' }
raise 'duplicate startup_script' unless matches.size <= 1
if matches.empty?
  puts JSON.generate({exists: false, value: nil})
else
  key, value = matches.first
  raise 'startup_script must be a string or null' unless value.is_a?(Psych::Nodes::Scalar)
  raise 'unsupported YAML tag' unless value.tag.nil? || ['tag:yaml.org,2002:str', 'tag:yaml.org,2002:null'].include?(value.tag)
  original = value.value
  original = nil if value.tag == 'tag:yaml.org,2002:null' || (value.plain && value.tag != 'tag:yaml.org,2002:str' && ['', '~', 'null', 'Null', 'NULL'].include?(original))
  finish = value.end_line + (value.end_column > 0 ? 1 : 0)
  puts JSON.generate({exists: true, value: original, first: key.start_line, finish: finish})
end
'''


def fail(message):
    raise RuntimeError(message)


def read_regular(path, limit=4 * 1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit or info.st_mode & 0o022:
            fail('文件不是当前用户拥有的安全普通文件：' + str(path))
        return handle.read(), stat.S_IMODE(info.st_mode)


def atomic_write(path, contents, mode=0o600):
    if path.is_symlink():
        fail('拒绝替换符号链接：' + str(path))
    if path.exists():
        read_regular(path, max(len(contents), 64 * 1024 * 1024))
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_field(contents):
    result = subprocess.run(['/usr/bin/ruby', '-W0', '--disable-gems', '-e', RUBY_FIELD],
                            input=contents, capture_output=True, timeout=8,
                            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'RUBYOPT': '', 'RUBYLIB': ''})
    if result.returncode:
        fail('无法安全解析 startup_script；未修改 Clash 配置。')
    return json.loads(result.stdout)


def patch_field(contents, value, remove=False):
    field = read_field(contents)
    lines = contents.decode('utf-8').splitlines(keepends=True)
    newline = '\r\n' if b'\r\n' in contents else '\n'
    replacement = '' if remove else 'startup_script: ' + json.dumps(value, ensure_ascii=False) + newline
    if field['exists']:
        lines[field['first']:field['finish']] = [replacement] if replacement else []
    elif not remove:
        insertion = next((index for index, line in enumerate(lines) if line.strip() == '...'), len(lines))
        if insertion and not lines[insertion - 1].endswith('\n'):
            lines[insertion - 1] += newline
        lines.insert(insertion, replacement)
    result = ''.join(lines).encode('utf-8')
    verified = read_field(result)
    if verified['exists'] == remove or (not remove and verified['value'] != value):
        fail('启动脚本字段回读验证失败。')
    return result


def launcher(root, previous):
    lines = [
        '#!/bin/bash',
        'runtime_root=' + shlex.quote(str(root)),
        'export CLASH_RUNTIME_BROKER_ROOT="$runtime_root"',
        'broker="$runtime_root/ClashRuntimeBroker"',
        'log_file="$runtime_root/bridge.log"',
        'if [ -x "$broker" ]; then',
        '  if [ -f "$log_file" ] && [ "$(/usr/bin/stat -f %z "$log_file" 2>/dev/null || printf 0)" -gt 1048576 ]; then',
        '    /bin/mv -f "$log_file" "$log_file.1"',
        '  fi',
        '  /usr/bin/nohup "$broker" </dev/null >>"$log_file" 2>&1 &',
        'fi',
    ]
    if previous:
        suffix = Path(previous).suffix
        interpreter = 'bash' if suffix == '.sh' else 'powershell' if suffix in ('.bat', '.ps1') else None
        if interpreter is None or not Path(previous).is_absolute():
            fail('已有启动脚本的格式或路径不受支持，未覆盖原设置。')
        lines += [
            'original_script=' + shlex.quote(previous),
            'if [ -f "$original_script" ]; then',
            '  (cd -- ' + shlex.quote(str(Path(previous).parent)) + ' && ' + interpreter + ' "$original_script")',
            '  exit $?',
            'fi',
        ]
    lines += ['exit 0', '']
    return '\n'.join(lines).encode('utf-8')


def command(binary, arguments, root, check=True):
    environment = {**os.environ, 'CLASH_RUNTIME_BROKER_ROOT': str(root)}
    environment.pop('CLASH_RUNTIME_BROKER_DISABLED', None)
    result = subprocess.run([str(binary), *arguments], capture_output=True, text=True, timeout=22, env=environment)
    if check and result.returncode:
        fail(result.stderr.strip() or '后台程序命令失败。')
    return result


def running_status(binary, root):
    result = command(binary, ['--status'], root, check=False)
    if result.returncode:
        return None
    return json.loads(result.stdout)


def stop_broker(binary, root):
    descriptor = os.open(root / 'broker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail('后台锁文件权限无效。')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if not binary.exists() or running_status(binary, root) is None:
                fail('旧后台进程仍占用锁，但状态接口不可用；保留安装，未覆盖或删除文件。')
        command(binary, ['--shutdown'], root)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                time.sleep(0.05)
        fail('后台桥接尚未释放锁，保留当前安装。')
    except BaseException:
        os.close(descriptor)
        raise


def recover_application(config, binary, root):
    recovery_path = root / 'clash-restart.json'
    if not recovery_path.exists():
        return
    recovery = json.loads(read_regular(recovery_path)[0])
    if recovery.get('schemaVersion') != 1 or recovery.get('config') != str(DEFAULT_CONFIG.resolve()) or not recovery.get('wasRunning'):
        fail('Clash 重启恢复记录无效，停止安装。')
    if config.resolve() != DEFAULT_CONFIG.resolve():
        fail('存在真实 Clash 的待恢复事务，不能用测试配置接管。')
    command(binary, ['--launch-clash'], root)
    recovery_path.unlink()


def activate_config(config, expected, updated, binary, root, restart):
    restore_application = False
    real_config = config.resolve() == DEFAULT_CONFIG.resolve()
    try:
        if real_config and json.loads(command(binary, ['--clash-running'], root).stdout)['running']:
            if not restart:
                fail('Clash 正在运行；请使用 --restart-clash 完成一次有序重启，避免旧内存设置覆盖配置。')
            recovery = {'schemaVersion': 1, 'config': str(DEFAULT_CONFIG.resolve()), 'wasRunning': True}
            atomic_write(root / 'clash-restart.json', json.dumps(recovery).encode())
            restore_application = True
            command(binary, ['--quit-clash'], root)
        current, mode = read_regular(config)
        if current != expected:
            previous_field, current_field, desired_field = read_field(expected), read_field(current), read_field(updated)
            if (previous_field['exists'], previous_field['value']) != (current_field['exists'], current_field['value']):
                fail('Clash 启动脚本设置已变化，保留用户选择；请重试安装。')
            updated = patch_field(current, desired_field['value'], remove=not desired_field['exists'])
        if real_config and json.loads(command(binary, ['--clash-running'], root).stdout)['running']:
            fail('Clash 已重新启动，未修改配置。')
        if read_regular(config)[0] != current:
            fail('Clash 配置在提交前再次变化，未覆盖新内容。')
        atomic_write(config, updated, mode)
        if read_regular(config)[0] != updated:
            fail('Clash 配置写入后校验失败。')
    finally:
        if restore_application:
            command(binary, ['--launch-clash'], root)
            (root / 'clash-restart.json').unlink(missing_ok=True)


def install(options):
    root, config = options.root, options.config
    source = options.app / 'Contents/MacOS/ClashRuntimeBroker'
    source_bytes, _ = read_regular(source, 64 * 1024 * 1024)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(source)], check=True, capture_output=True)
    original_config, _ = read_regular(config)
    current = read_field(original_config)
    wrapper = root / 'start-clash-bridge.sh'
    binary = root / 'ClashRuntimeBroker'
    receipt_path = root / 'installation.json'
    receipt = json.loads(read_regular(receipt_path)[0]) if receipt_path.exists() else None
    if receipt is not None and (receipt.get('schemaVersion') != 1 or receipt.get('wrapper') != str(wrapper)):
        fail('安装记录不兼容，未修改现有配置。')
    if current['value'] == str(wrapper):
        if receipt is None:
            fail('启动器已配置但缺少原设置备份，停止覆盖。')
        previous = receipt['previousStartupScript']
        previous_exists = receipt['previousFieldExists']
    elif receipt is not None and (receipt.get('everActivated') or receipt.get('phase') == 'active'):
        fail('用户已更改启动脚本；保留用户选择，不自动重新启用后台程序。')
    elif receipt is not None:
        if (current['exists'], current['value']) != (receipt['previousFieldExists'], receipt['previousStartupScript']):
            fail('准备安装后启动脚本已被更改；保留用户选择。')
        previous, previous_exists = receipt['previousStartupScript'], receipt['previousFieldExists']
    else:
        previous, previous_exists = current['value'], current['exists']
    script = launcher(root, previous)
    desired_receipt = {
        'schemaVersion': 1, 'wrapper': str(wrapper), 'previousStartupScript': previous,
        'previousFieldExists': previous_exists, 'phase': 'prepared',
        'everActivated': bool(receipt and (receipt.get('everActivated') or receipt.get('phase') == 'active')) or current['value'] == str(wrapper),
        'brokerSHA256': hashlib.sha256(source_bytes).hexdigest(),
    }
    stopped_lock = None
    if not binary.exists() or read_regular(binary, 64 * 1024 * 1024)[0] != source_bytes:
        stopped_lock = stop_broker(binary, root)
    try:
        atomic_write(binary, source_bytes, 0o700)
        atomic_write(wrapper, script, 0o700)
        atomic_write(receipt_path, (json.dumps(desired_receipt, ensure_ascii=False, indent=2) + '\n').encode())
    finally:
        if stopped_lock is not None:
            os.close(stopped_lock)
    if current['value'] != str(wrapper):
        activate_config(config, original_config, patch_field(original_config, str(wrapper)), binary, root, options.restart_clash)
    desired_receipt['phase'] = 'active'
    desired_receipt['everActivated'] = True
    atomic_write(receipt_path, (json.dumps(desired_receipt, ensure_ascii=False, indent=2) + '\n').encode())
    if running_status(binary, root) is None:
        environment = {**os.environ, 'CLASH_RUNTIME_BROKER_ROOT': str(root)}
        environment.pop('CLASH_RUNTIME_BROKER_DISABLED', None)
        subprocess.Popen([str(binary)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=environment)
    deadline = time.monotonic() + 4
    status = None
    while time.monotonic() < deadline:
        status = running_status(binary, root)
        if status is not None:
            break
        time.sleep(0.1)
    if status is None:
        fail('启动脚本已保存，但后台桥接未就绪；请检查 Runtime/bridge.log。')
    print(json.dumps({'startupEnabled': True, 'previousScriptPreserved': bool(previous),
                      'brokerRunning': True, 'attached': status.get('attached') is True,
                      'pid': status.get('pid')}, ensure_ascii=False))


def uninstall(options):
    root, config = options.root, options.config
    receipt = json.loads(read_regular(root / 'installation.json')[0])
    wrapper = root / 'start-clash-bridge.sh'
    binary = root / 'ClashRuntimeBroker'
    if receipt.get('schemaVersion') != 1 or receipt.get('wrapper') != str(wrapper):
        fail('安装记录不兼容，未执行卸载。')
    original_config, _ = read_regular(config)
    current = read_field(original_config)
    restored = current['value'] == str(wrapper)
    stopped_lock = stop_broker(binary, root)
    try:
        if restored:
            replacement = patch_field(original_config, receipt['previousStartupScript'], remove=not receipt['previousFieldExists'])
            activate_config(config, original_config, replacement, binary, root, options.restart_clash)
        for name in ('start-clash-bridge.sh', 'ClashRuntimeBroker', 'installation.json', 'state.json', 'bridge.log', 'bridge.log.1'):
            target = root / name
            if target.exists() or target.is_symlink():
                read_regular(target, 64 * 1024 * 1024)
                target.unlink()
    finally:
        os.close(stopped_lock)
    print(json.dumps({'uninstalled': True, 'previousSettingRestored': restored}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path)
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('CLASH_RUNTIME_BROKER_ROOT', DEFAULT_ROOT)))
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--restart-clash', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    options = parser.parse_args()
    if not options.uninstall and options.app is None:
        parser.error('--app is required for installation')
    if not options.root.is_absolute() or not options.config.is_absolute():
        fail('安装目录和配置路径必须为绝对路径。')
    if options.root.is_symlink():
        fail('拒绝使用符号链接作为后台目录。')
    options.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = options.root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail('后台目录必须为当前用户的私有目录。')
    lock_fd = os.open(options.root / 'installation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077:
            fail('安装锁文件权限无效。')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover_application(options.config, options.root / 'ClashRuntimeBroker', options.root)
        if options.uninstall:
            uninstall(options)
        else:
            install(options)
    finally:
        os.close(lock_fd)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('后台桥接安装失败：' + str(error), file=sys.stderr)
        sys.exit(1)
