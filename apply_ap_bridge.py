#!/usr/bin/env python3
"""Apply MTIAA bridge v6 to clean source (build-time only).

v6 preserves v5 persistence and adds Radio, Tuning Fork and speech recipe.
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
void APBridgeGameplayReady();
int APBridgeInterceptRecipeFlag(int stateId, int instruction);
#endif
'''
CPP = r'''#include "APBridge.h"
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "GameState.h"
#include "GameEngine.h"
#include "SCI_Inventory.h"
#include "globals.h"

void SendGameMessage(int, int, int, int, int, int, int, int, int, int);
static char s_world[65] = "";
static char s_session[64] = "";
static int s_bound = 0;
static int s_worldConflict = 0;
static int s_ready = 0;
static int s_generation = 0;
static int s_queuedGrantId = 0;
static int s_queuedSequence = 0;
static int s_activeGrantId = 0;
static int s_activeSequence = 0;

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
    char path[MAX_PATH]; FILE* file;
    if (!APBridgePath(path, leaf)) return;
    file = fopen(path, "a");
    if (file == 0) return;
    fprintf(file, "%s\n", line);
    fclose(file);
}
static int APBridgeBinding(char* buffer) {
    char path[MAX_PATH]; FILE* file; int i;
    if (!APBridgePath(path, "binding.txt")) return 0;
    file = fopen(path, "rb");
    if (file == 0) return 0;
    buffer[0] = 0;
    if (fgets(buffer, 68, file) == 0) { fclose(file); return 0; }
    fclose(file);
    // The client writes the first 32 hex characters of SHA256.
    if (strlen(buffer) < 32) return 0;
    for (i = 0; i < 32; ++i) {
        char c = buffer[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
    }
    buffer[32] = 0;
    return 1;
}
static void APBridgeEnsureSession() {
    char newWorld[68]; char line[200]; FILETIME now;
    if (!APBridgeBinding(newWorld)) return;
    if (s_bound) {
        if (strcmp(s_world, newWorld) != 0 && !s_worldConflict) {
            s_worldConflict = 1;
            s_ready = 0;
            APBridgeAppend("debug.log", "WORLD_CHANGED_RESTART_GAME_REQUIRED");
        }
        return;
    }
    strcpy(s_world, newWorld);
    GetSystemTimeAsFileTime(&now);
    sprintf(s_session, "%lu-%lu-%lu", (unsigned long)GetCurrentProcessId(),
        (unsigned long)now.dwHighDateTime, (unsigned long)now.dwLowDateTime);
    s_bound = 1;
    sprintf(line, "SESSION %s %s %d", s_world, s_session, s_generation);
    APBridgeAppend("events.log", line);
    APBridgeAppend("debug.log", "SESSION_STARTED_V5");
}
void APBridgeGameplayReady() {
    char line[200];
    APBridgeEnsureSession();
    if (!s_bound || s_worldConflict || s_ready) return;
    s_ready = 1;
    sprintf(line, "READY %s %s %d", s_world, s_session, s_generation);
    APBridgeAppend("events.log", line);
}
void APBridgeStoryReloaded() {
    char line[200];
    APBridgeEnsureSession();
    if (!s_bound || s_worldConflict) return;
    ++s_generation;
    s_ready = 0;
    s_queuedGrantId = 0;
    s_queuedSequence = 0;
    s_activeGrantId = 0;
    s_activeSequence = 0;
    sprintf(line, "LOAD %s %s %d", s_world, s_session, s_generation);
    APBridgeAppend("events.log", line);
    APBridgeAppend("debug.log", "GAME_LOAD_COMPLETE_V5");
    // GameEngine::Serialize finished loading all the handlers.
    APBridgeGameplayReady();
}
int APBridgeInterceptRecipeFlag(int stateId, int instruction) {
    // QUEST19.MIS QUESTION2507 turns on GAMESTATE 77. Give a location
    // check, not the vanilla recipe unlock. AP delivery unlocks it instead.
    if (stateId != 77 || instruction != 0xF) return 0;
    APBridgeSendCheck(1005);
    return 1;
}
void APBridgeSendCheck(int locationId) {
    char line[200];
    APBridgeEnsureSession();
    if (!s_bound || s_worldConflict || !s_ready) {
        APBridgeAppend("debug.log", "CHECK_BLOCKED_NOT_READY");
        return;
    }
    sprintf(line, "CHECK %s %s %d", s_world, s_session, locationId);
    APBridgeAppend("events.log", line);
}
int APBridgeConsumeGrant(int itemId) {
    if (!s_ready || s_worldConflict || s_queuedGrantId != itemId) return 0;
    s_activeGrantId = itemId;
    s_activeSequence = s_queuedSequence;
    s_queuedGrantId = 0;
    s_queuedSequence = 0;
    return 1;
}
void APBridgeGrantFinished(int itemId, int alreadyPresent) {
    char line[220];
    if (!s_bound || s_worldConflict || s_activeGrantId != itemId || s_activeSequence <= 0) return;
    sprintf(line, "APPLIED %s %s %d %d %d %d", s_world, s_session,
        s_generation, s_activeSequence, itemId, alreadyPresent);
    APBridgeAppend("applied.log", line);
    s_activeGrantId = 0;
    s_activeSequence = 0;
}
void APBridgePoll() {
    char inboxPath[MAX_PATH], line[220], command[16], world[65], session[64];
    int itemId = 0, seq = 0, generation = 0;
    FILE* input;
    APBridgeEnsureSession();
    if (!s_bound || !s_ready || s_worldConflict || s_queuedGrantId || s_activeGrantId) return;
    if (!APBridgePath(inboxPath, "inbox.txt")) return;
    input = fopen(inboxPath, "rb");
    if (input == 0) return;
    line[0] = 0;
    if (fgets(line, sizeof(line), input) == 0) { fclose(input); return; }
    fclose(input);
    command[0] = 0; world[0] = 0; session[0] = 0;
    if (sscanf(line, " %15s %64s %63s %d %d %d", command, world, session,
               &generation, &itemId, &seq) != 6 || strcmp(command, "GRANT") ||
               itemId < 1 || itemId > 22 || seq <= 0) {
        APBridgeAppend("debug.log", "INBOX_INVALID_FORMAT");
        return;
    }
    if (strcmp(world, s_world) || strcmp(session, s_session) || generation != s_generation) {
        // Stale commands can never be applied to a new world/session/load.
        if (remove(inboxPath) == 0) APBridgeAppend("debug.log", "STALE_GRANT_DISCARDED");
        return;
    }
    if (remove(inboxPath) != 0) return;
    s_queuedGrantId = itemId;
    s_queuedSequence = seq;
    if (itemId == 22) {
        // Item 22 is a virtual AP recipe. The original game knowledge flag
        // is 77; there is no INVENTOR item 22 and no new .MIS needed.
        GameState* gs = g_GameState_0046aa30;
        if (gs == 0 || gs->stateValues == 0 || gs->maxStates <= 77) {
            s_queuedGrantId = 0;
            s_queuedSequence = 0;
            APBridgeAppend("debug.log", "RECIPE_GAMESTATE_NOT_READY");
            return;
        }
        s_activeGrantId = s_queuedGrantId;
        s_activeSequence = s_queuedSequence;
        s_queuedGrantId = 0;
        s_queuedSequence = 0;
        {
            int alreadyPresent = gs->stateValues[77] != 0;
            gs->stateValues[77] = 1;
            // If all three ingredients were already found, the recipe is the
            // last requirement. Reuse the game's native AUTOCOMBINE panels.
            GameEngine* engine = g_GameEngine_0046a6ec;
            if (engine != 0) {
                SCI_Inventory* inv = (SCI_Inventory*)engine->FindHandlerInList(0x1E);
                if (inv != 0) inv->DisplayPanels(0);
            }
            APBridgeGrantFinished(itemId, alreadyPresent);
        }
        return;
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
    school_path, school, school_eol = load('SCI_SchoolMenu.cpp')
    state_path, state, state_eol = load('GameState.cpp')
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
        '        if (!apGranted && (itemId == 5 || itemId == 7 || itemId == 2 || itemId == 8)) {\n'
        '            T_Object* pickup = (T_Object*)FindItem(itemId);\n'
        '            if (pickup != 0) pickup->Reset();\n'
        '            APBridgeSendCheck(itemId == 5 ? 1001 : itemId == 7 ? 1002 : itemId == 2 ? 1003 : 1004);\n'
        '            break;\n'
        '        }\n'
        '        // Completed Disrupt Speech Test represents consumed ingredients.\n'
        '        if (apGranted && (itemId == 2 || itemId == 7 || itemId == 8) &&\n'
        '            FindItemInList(18) != 0) {\n'
        '            APBridgeGrantFinished(itemId, 1);\n'
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
    state = change_once(state, '#include "GameState.h"\n',
        '#include "GameState.h"\n#include "APBridge.h"\n', 'Gamestate bridge include')
    state = change_once(state,
        '    switch (action->instruction) {\n',
        '    if (APBridgeInterceptRecipeFlag(idx, action->instruction)) return;\n'
        '    switch (action->instruction) {\n', 'Recipe flag interception')
    # A saved game's inventory handler is optional. Hook after all handlers loaded.
    engine = change_once(engine,
        '        h->Serialize(param);\n    }\n}',
        '        h->Serialize(param);\n    }\n    APBridgeStoryReloaded();\n}',
        'Completed game load notification')
    school = change_once(school,
        '    g_SchoolMenuActive_0046a190 = 1;\n',
        '    g_SchoolMenuActive_0046a190 = 1;\n    APBridgeGameplayReady();\n',
        'School gameplay-ready notification')
    school = change_once(school, '#include "string.h"\n',
        '#include "string.h"\n#include "APBridge.h"\n', 'School bridge include')
    engine_path.write_bytes(engine.replace('\n', eng_eol).encode('utf-8'))
    school_path.write_bytes(school.replace('\n', school_eol).encode('utf-8'))
    inventory_path.write_bytes(inventory.replace('\n', inv_eol).encode('utf-8'))
    state_path.write_bytes(state.replace('\n', state_eol).encode('utf-8'))
    bridge_h.write_text(HEADER, encoding='utf-8')
    bridge_cpp.write_text(CPP, encoding='utf-8')
    print('AP bridge v6 applied: five locations including recipe, persistence and receipts')


if __name__ == '__main__':
    main()
