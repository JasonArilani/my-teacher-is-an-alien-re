#!/usr/bin/env python3
"""Build-time injector for MTIAA AP bridge v2.

Replaces the proof-of-concept injector for the ap-prototype GitHub Actions build.
No retail data or vanilla game executable is altered.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HEADER = r'''#ifndef MTIAA_AP_BRIDGE_H
#define MTIAA_AP_BRIDGE_H

void APBridgePoll();
void APBridgeSendCheck(int locationId);
int APBridgeConsumeGrant(int itemId);

#endif
'''

CPP = r'''#include "APBridge.h"
#include <windows.h>
#include <stdio.h>
#include <string.h>

// Engine message system; the inventory handler is 0x1E.
void SendGameMessage(int, int, int, int, int, int, int, int, int, int);

static int s_queuedGrantId = 0;
static int s_pollStarted = 0;
static int s_invalidAlreadyLogged = 0;
static int s_removeAlreadyLogged = 0;

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

static void APBridgeDebug(const char* message) {
    char path[MAX_PATH];
    FILE* output;
    if (!APBridgePath(path, "debug.log")) return;
    output = fopen(path, "a");
    if (output == 0) return;
    fprintf(output, "%s\n", message);
    fclose(output);
}

void APBridgeSendCheck(int locationId) {
    char path[MAX_PATH];
    FILE* output;
    if (!APBridgePath(path, "events.log")) return;
    output = fopen(path, "a");
    if (output == 0) return;
    fprintf(output, "CHECK %d\n", locationId);
    fclose(output);
    APBridgeDebug("CHECK_WRITTEN");
}

int APBridgeConsumeGrant(int itemId) {
    if (s_queuedGrantId != itemId) return 0;
    s_queuedGrantId = 0;
    APBridgeDebug("GRANT_HANDLER_REACHED");
    return 1;
}

void APBridgePoll() {
    char path[MAX_PATH];
    char queuedPath[MAX_PATH];
    char line[128];
    char command[16];
    char* start;
    int itemId = 0;
    int sequence = 0;
    FILE* input;
    FILE* output;

    if (!s_pollStarted) {
        s_pollStarted = 1;
        APBridgeDebug("POLL_STARTED");
    }
    if (s_queuedGrantId != 0) return;
    if (!APBridgePath(path, "inbox.txt")) return;
    input = fopen(path, "rb");
    if (input == 0) return;
    if (fgets(line, sizeof(line), input) == 0) {
        fclose(input);
        return;
    }
    fclose(input);

    start = line;
    // Accept the optional UTF-8 BOM produced by some text editors.
    if ((unsigned char)start[0] == 0xEF &&
        (unsigned char)start[1] == 0xBB &&
        (unsigned char)start[2] == 0xBF) {
        start += 3;
    }
    command[0] = 0;
    if (sscanf(start, " %15s %d %d", command, &itemId, &sequence) != 3 ||
        strcmp(command, "GRANT") != 0 || itemId < 1 || itemId > 21 || sequence < 0) {
        if (!s_invalidAlreadyLogged) {
            APBridgeDebug("INBOX_INVALID_FORMAT");
            s_invalidAlreadyLogged = 1;
        }
        return;
    }
    s_invalidAlreadyLogged = 0;

    if (remove(path) != 0) {
        if (!s_removeAlreadyLogged) {
            APBridgeDebug("INBOX_REMOVE_FAILED");
            s_removeAlreadyLogged = 1;
        }
        return;
    }
    s_removeAlreadyLogged = 0;

    // This acknowledges dispatch, NOT successful in-game possession.
    s_queuedGrantId = itemId;
    APBridgeDebug("GRANT_PARSED_AND_QUEUED");
    if (APBridgePath(queuedPath, "queued.log")) {
        output = fopen(queuedPath, "a");
        if (output != 0) {
            fprintf(output, "QUEUED %d %d\n", sequence, itemId);
            fclose(output);
        }
    }
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
        raise RuntimeError('APBridge files already exist. Start from a clean source checkout.')

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
        '        // Prototype AP location: Susan\'s Tape Player (object 5).\n'
        '        // Complete the pickup flag (OBJ005), but do not award its item.\n'
        '        // A grant from the AP bridge bypasses this location check.\n'
        '        if (itemId == 5 && !APBridgeConsumeGrant(itemId)) {\n'
        '            T_Object* pickup = (T_Object*)FindItem(itemId);\n'
        '            if (pickup != 0) pickup->Reset();\n'
        '            APBridgeSendCheck(1001);\n'
        '            break;\n'
        '        }\n'
        '        if (itemId != 5) APBridgeConsumeGrant(itemId);\n\n'
        '        if (FindItemInList(itemId) != 0) break;', 'Inventory insert')

    # Prepare all results before mutating the checkout.
    engine_bytes = engine.replace('\n', eng_eol).encode('utf-8')
    inventory_bytes = inventory.replace('\n', inv_eol).encode('utf-8')
    engine_path.write_bytes(engine_bytes)
    inventory_path.write_bytes(inventory_bytes)
    bridge_h.write_text(HEADER, encoding='utf-8')
    bridge_cpp.write_text(CPP, encoding='utf-8')
    print('AP bridge v2 applied: pickup completion and command diagnostics')


if __name__ == '__main__':
    main()
