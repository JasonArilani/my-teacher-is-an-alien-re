#!/usr/bin/env python3
"""Apply MTIAA bridge v4 to clean source (build-time only).

v4 protocol adds per-process session / save-load generation and APPLIED receipts.
Existing gameplay, checks, item IDs and original pickup flags remain as in v3.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HEADER = r'''#ifndef MTIAA_AP_BRIDGE_H
#define MTIAA_AP_BRIDGE_H

void APBridgePoll();
void APBridgeSendCheck(int locationId);
int APBridgeConsumeGrant(int itemId);
void APBridgeGrantFinished(int itemId, int alreadyPresent);
void APBridgeStoryReloaded();

#endif
'''

CPP = r'''#include "APBridge.h"
#include <windows.h>
#include <stdio.h>
#include <string.h>

void SendGameMessage(int, int, int, int, int, int, int, int, int, int);

static int s_queuedGrantId = 0;
static int s_grantSequence = 0;
static int s_activeGrantId = 0;
static int s_activeSequence = 0;
static int s_sessionStarted = 0;
static char s_session[64] = "";
static int s_generation = 0;

static int APBridgePath(char* output, const char* leaf) {
    DWORD length = GetModuleFileNameA(NULL, output, MAX_PATH);
    char* slash;
    if (length == 0 || length >= MAX_PATH) return 0;
    slash = strrchr(output, '\\');
    if (slash == 0) return 0;
    *slash = 0;
    if (strlen(output) + strlen("\\DATA\\apbridge") + 1 >= MAX_PATH) return 0;
    strcat(output, "\\DATA\\apbridge");
    CreateDirectoryA(output, NULL);
    if (leaf != 0) {
        if (strlen(output) + strlen(leaf) + 2 >= MAX_PATH) return 0;
        strcat(output, "\\");
        strcat(output, leaf);
    }
    return 1;
}

static void APBridgeAppend(const char* leaf, const char* line) {
    char path[MAX_PATH];
    FILE* file;
    if (!APBridgePath(path, leaf)) return;
    file = fopen(path, "a");
    if (file == 0) return;
    fprintf(file, "%s\n", line);
    fclose(file);
}

static void APBridgeEnsureSession() {
    FILETIME now;
    char line[128];
    if (s_sessionStarted) return;
    GetSystemTimeAsFileTime(&now);
    sprintf(s_session, "%lu-%lu-%lu",
        (unsigned long)GetCurrentProcessId(),
        (unsigned long)now.dwHighDateTime,
        (unsigned long)now.dwLowDateTime);
    sprintf(line, "SESSION %s %d", s_session, s_generation);
    s_sessionStarted = 1;
    APBridgeAppend("events.log", line);
    APBridgeAppend("debug.log", "SESSION_STARTED");
}

void APBridgeStoryReloaded() {
    char line[128];
    // Called after the native inventory load finishes, not after a save.
    ++s_generation;
    if (!s_sessionStarted) return;
    sprintf(line, "LOAD %s %d", s_session, s_generation);
    APBridgeAppend("events.log", line);
    APBridgeAppend("debug.log", "GAME_INVENTORY_RELOADED");
}

void APBridgeSendCheck(int locationId) {
    char line[80];
    APBridgeEnsureSession();
    sprintf(line, "CHECK %d", locationId);
    APBridgeAppend("events.log", line);
}

int APBridgeConsumeGrant(int itemId) {
    if (s_queuedGrantId != itemId) return 0;
    s_activeGrantId = itemId;
    s_activeSequence = s_grantSequence;
    s_queuedGrantId = 0;
    s_grantSequence = 0;
    return 1;
}

void APBridgeGrantFinished(int itemId, int alreadyPresent) {
    char line[160];
    if (s_activeGrantId != itemId || s_activeSequence <= 0) return;
    sprintf(line, "APPLIED %s %d %d %d %d", s_session, s_generation,
        s_activeSequence, itemId, alreadyPresent);
    APBridgeAppend("applied.log", line);
    s_activeGrantId = 0;
    s_activeSequence = 0;
}

void APBridgePoll() {
    char inboxPath[MAX_PATH];
    char line[128];
    char command[16];
    char* start;
    int itemId = 0;
    int sequence = 0;
    FILE* input;
    APBridgeEnsureSession();
    if (s_queuedGrantId != 0 || s_activeGrantId != 0) return;
    if (!APBridgePath(inboxPath, "inbox.txt")) return;
    input = fopen(inboxPath, "rb");
    if (input == 0) return;
    if (fgets(line, sizeof(line), input) == 0) {
        fclose(input);
        return;
    }
    fclose(input);
    start = line;
    if ((unsigned char)start[0] == 0xEF &&
        (unsigned char)start[1] == 0xBB &&
        (unsigned char)start[2] == 0xBF) start += 3;
    command[0] = 0;
    if (sscanf(start, " %15s %d %d", command, &itemId, &sequence) != 3 ||
        strcmp(command, "GRANT") != 0 || itemId < 1 || itemId > 21 || sequence <= 0) {
        APBridgeAppend("debug.log", "INBOX_INVALID_FORMAT");
        return;
    }
    if (remove(inboxPath) != 0) {
        APBridgeAppend("debug.log", "INBOX_REMOVE_FAILED");
        return;
    }
    s_queuedGrantId = itemId;
    s_grantSequence = sequence;
    SendGameMessage(0x1E, itemId, 0x1E, 0, 0x17, 0, 0, 0, 0, 0);
}
'''


def load(name):
    path = ROOT / 'src' / name
    raw = path.read_bytes()
    eol = '\r\n' if b'\r\n' in raw else '\n'
    return path, raw.decode('utf-8').replace('\r\n', '\n'), eol


def change_once(value, old, new, label):
    count = value.count(old)
    if count != 1:
        raise RuntimeError('%s: expected one source anchor but found %d' % (label, count))
    return value.replace(old, new, 1)


def main():
    engine_path, engine, eng_eol = load('GameEngine.cpp')
    inventory_path, inventory, inv_eol = load('SCI_Inventory.cpp')
    bridge_h = ROOT / 'src' / 'APBridge.h'
    bridge_cpp = ROOT / 'src' / 'APBridge.cpp'
    if bridge_h.exists() or bridge_cpp.exists():
        raise RuntimeError('APBridge files already exist. Start from a clean checkout.')

    engine = change_once(engine, '#include "GameLoopHelper.h"\n',
                         '#include "GameLoopHelper.h"\n#include "APBridge.h"\n', 'Engine include')
    engine = change_once(engine,
        '            ProcessInput();\n            if (m_exitFlag != 0) break;\n            ProcessEvents();',
        '            ProcessInput();\n            if (m_exitFlag != 0) break;\n            APBridgePoll();\n            ProcessEvents();', 'Engine loop')
    inventory = change_once(inventory, '#include "MouseControl.h"\n',
                            '#include "MouseControl.h"\n#include "APBridge.h"\n', 'Inventory include')
    inventory = change_once(inventory,
        '        itemId = action->addressValue;\n        if (itemId == 0) break;\n\n'
        '        if (FindItemInList(itemId) != 0) break;',
        '        itemId = action->addressValue;\n        if (itemId == 0) break;\n\n'
        '        // AP ownership and the physical pickup are separate.\n'
        '        int apGranted = APBridgeConsumeGrant(itemId);\n'
        '        if (!apGranted && (itemId == 5 || itemId == 7)) {\n'
        '            T_Object* pickup = (T_Object*)FindItem(itemId);\n'
        '            if (pickup != 0) pickup->Reset();\n'
        '            APBridgeSendCheck(itemId == 5 ? 1001 : 1002);\n'
        '            break;\n'
        '        }\n'
        '        if (FindItemInList(itemId) != 0) {\n'
        '            if (apGranted) APBridgeGrantFinished(itemId, 1);\n'
        '            break;\n'
        '        }', 'Inventory add/receipt')
    inventory = change_once(inventory,
        '        item = (T_Object*)FindItem(itemId);\n        item->Reset();',
        '        item = (T_Object*)FindItem(itemId);\n'
        '        if (!apGranted) item->Reset();', 'Inventory pickup flag')
    inventory = change_once(inventory,
        '        DisplayPanels(itemId);\n        break;\n    }\n    case 0x18:',
        '        DisplayPanels(itemId);\n'
        '        if (apGranted) APBridgeGrantFinished(itemId, 0);\n'
        '        break;\n    }\n    case 0x18:', 'Inventory receipt acknowledged')
    inventory = change_once(inventory,
        '        fread((char*)&g_SelectedItem_0046a6e4->objectFlags, 4, 1, (FILE*)fp);\n'
        '    }\n}\n\n// Stubs (moved from stubs.cpp)',
        '        fread((char*)&g_SelectedItem_0046a6e4->objectFlags, 4, 1, (FILE*)fp);\n'
        '    }\n'
        '    APBridgeStoryReloaded();\n'
        '}\n\n// Stubs (moved from stubs.cpp)', 'Inventory load notification')
    engine_path.write_bytes(engine.replace('\n', eng_eol).encode('utf-8'))
    inventory_path.write_bytes(inventory.replace('\n', inv_eol).encode('utf-8'))
    bridge_h.write_text(HEADER, encoding='utf-8')
    bridge_cpp.write_text(CPP, encoding='utf-8')
    print('AP bridge v4 applied: session, inventory-load epoch and applied acknowledgments')


if __name__ == '__main__':
    main()
