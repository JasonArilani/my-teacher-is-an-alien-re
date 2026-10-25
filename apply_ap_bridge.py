#!/usr/bin/env python3
"""Install the MTIAA AP bridge prototype into the checked-out reconstruction.

Designed for GitHub Actions and the ap-prototype branch. The old .patch
contains the two new C++ files. This installer avoids fragile line-number /
line-ending matching while refusing to modify unexpected source versions.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
PATCH = ROOT / 'mtiaa-ap-prototype.patch'


def extract_new_file(patch_text, filename):
    pattern = (r'^diff --git a/' + re.escape(filename) + r' b/' + re.escape(filename)
               + r'\n(?P<section>.*?)(?=^diff --git |\Z)')
    result = re.search(pattern, patch_text, flags=re.MULTILINE | re.DOTALL)
    if result is None or 'new file mode' not in result.group('section'):
        raise RuntimeError('Expected new file is missing from patch: ' + filename)
    lines = result.group('section').splitlines()
    payload = []
    seen_hunk = False
    for line in lines:
        if line.startswith('@@ '):
            seen_hunk = True
        elif seen_hunk and line.startswith('+'):
            payload.append(line[1:])
        elif seen_hunk and line.startswith(' '):
            payload.append(line[1:])
        elif seen_hunk and line.startswith('\\ No newline'):
            pass
        elif seen_hunk and line.startswith('-'):
            raise RuntimeError('Unexpected deletion in new file ' + filename)
    if not seen_hunk:
        raise RuntimeError('No contents for ' + filename)
    return '\n'.join(payload) + '\n'


def replace_once(source, old, new, name):
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f'{name}: expected 1 anchor, found {count}: {old[:72]!r}')
    return source.replace(old, new, 1)


def read_cpp(name):
    path = ROOT / 'src' / name
    raw = path.read_bytes()
    style = '\r\n' if b'\r\n' in raw else '\n'
    normalized = raw.decode('utf-8').replace('\r\n', '\n')
    return path, normalized, style


def main():
    patch = PATCH.read_text(encoding='utf-8')
    h = extract_new_file(patch, 'src/APBridge.h')
    cpp = extract_new_file(patch, 'src/APBridge.cpp')
    engine_path, engine, engine_eol = read_cpp('GameEngine.cpp')
    inventory_path, inventory, inv_eol = read_cpp('SCI_Inventory.cpp')

    # Do not silently run this against an already-patched branch.
    if (ROOT / 'src' / 'APBridge.h').exists() or (ROOT / 'src' / 'APBridge.cpp').exists():
        raise RuntimeError('APBridge source already exists; do not apply twice.')

    engine = replace_once(
        engine,
        '#include "GameLoopHelper.h"\n',
        '#include "GameLoopHelper.h"\n#include "APBridge.h"\n',
        'GameEngine include')
    engine = replace_once(
        engine,
        '            ProcessInput();\n            if (m_exitFlag != 0) break;\n            ProcessEvents();',
        '            ProcessInput();\n            if (m_exitFlag != 0) break;\n            APBridgePoll();\n            ProcessEvents();',
        'GameEngine loop')
    inventory = replace_once(
        inventory,
        '#include "MouseControl.h"\n',
        '#include "MouseControl.h"\n#include "APBridge.h"\n',
        'Inventory include')
    inventory = replace_once(
        inventory,
        '        itemId = action->addressValue;\n        if (itemId == 0) break;\n\n'
        '        if (FindItemInList(itemId) != 0) break;',
        '        itemId = action->addressValue;\n        if (itemId == 0) break;\n\n'
        '        // Prototype: replace vanilla Tape Player insert with AP check.\n'
        '        if (itemId == 5 && !APBridgeConsumeGrant(itemId)) {\n'
        '            APBridgeSendCheck(1001);\n'
        '            break;\n'
        '        }\n'
        '        if (itemId != 5) APBridgeConsumeGrant(itemId);\n\n'
        '        if (FindItemInList(itemId) != 0) break;',
        'Inventory insertion')

    # All anchors checked before ANY source file is changed.
    engine_path.write_bytes(engine.replace('\n', engine_eol).encode('utf-8'))
    inventory_path.write_bytes(inventory.replace('\n', inv_eol).encode('utf-8'))
    (ROOT / 'src' / 'APBridge.h').write_bytes(h.encode('utf-8'))
    (ROOT / 'src' / 'APBridge.cpp').write_bytes(cpp.encode('utf-8'))
    print('AP bridge applied: GameEngine.cpp, SCI_Inventory.cpp, APBridge.h, APBridge.cpp')


if __name__ == '__main__':
    main()
