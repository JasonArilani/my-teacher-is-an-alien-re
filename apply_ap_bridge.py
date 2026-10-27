#!/usr/bin/env python3
"""MTIAA v9 stabilization prototype, based on tested character and period mechanics.

Apply at build-time to a clean reconstruction checkout.
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
void APBridgeNewGame();
void APBridgeGameplayReady();
int APBridgeInterceptRecipeFlag(int stateId, int instruction);
int APBridgeHasInventoryItem(int itemId);
int APBridgeBeginItemEvidence(int itemId, const char* flagName);
void APBridgeEndItemEvidence(int previousValue, const char* flagName);
void APBridgeLogMaintenanceSource(int apGranted, int apReturning);
int APBridgeCharacterUnlocked(int characterIndex);
int APBridgeCurrentCharacterUnlocked();
int APBridgeCurrentCharacterCanGo();
int APBridgeMaxPeriod();
int APBridgeCanEnterNextPeriod();
void APBridgePrepareChoiceMenu();
void APBridgeOnFocusChange(int toType, int toValue, int fromType, int fromValue);
void APBridgeTraceStateAction(int stateId, int instruction);
#endif
'''
CPP = r'''#include "APBridge.h"
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "GameState.h"
#include "GameEngine.h"
#include "SCI_Inventory.h"
#include "T_Object.h"
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
static int s_lastState[450];
static int s_haveBaseline = 0;
static unsigned char s_sentEvent[1100];
// AP-only character access. Bit 0 Peter, bit 1 Susan, bit 2 Duncan.
// Peter starts enabled in this proof of concept; story flags are untouched.
static int s_charUnlockMask = 1;
// Replayed on every New Game or Load from uniquely sequenced AP receipts.
static int s_progressivePeriods = 0;
static int s_lastPeriodBlocked = -1;
// Diagnostic build only: bounded per-New-Game trace for all outstanding checks.
// Kept in a separate log so normal AP checks and debug.log remain readable.
static int s_traceLines = 0;
static const int kMaxTraceLines = 12000;

static void APBridgeWatchStates();
static void APBridgeAppend(const char* filename, const char* line);

static void APBridgeTraceLine(const char* line) {
    if (!s_bound || !s_ready || s_worldConflict || s_traceLines >= kMaxTraceLines) return;
    ++s_traceLines;
    APBridgeAppend("ap_events_trace.log", line);
    if (s_traceLines == kMaxTraceLines)
        APBridgeAppend("debug.log", "AP_TRACE_CAP_REACHED_RESTART_NEW_GAME_FOR_MORE");
}

// State-changing MIS commands sometimes set and clear a flag within the
// same game-loop frame; the polling watcher alone would miss those attempts.
// This only reports the command; it never modifies or consumes the action.
void APBridgeTraceStateAction(int stateId, int instruction) {
    GameState* gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0 || gs->maxStates <= 99 ||
        stateId < 0 || stateId >= gs->maxStates || stateId >= 450) return;
    // Repeated identical writes can dominate puzzle animations; the hard cap
    // also bounds disk usage over long sessions.
    char line[180];
    sprintf(line, "AP_TRACE_ACTION idx=%d op=%d before=%d period=%d kid=%d",
            stateId, instruction, gs->stateValues[stateId],
            gs->stateValues[60], gs->stateValues[99]);
    APBridgeTraceLine(line);
}

// The original pod minigame tests OBJ011, the vanilla pickup flag, rather
// than checking the actual shared inventory. AP grants deliberately do NOT
// set pickup flags (the physical location must remain available as a check).
// This helper lets SC_Pods temporarily interpret an AP-owned Ray Gun as
// equipped while it parses cb_pods.mis, without consuming that location.
int APBridgeHasInventoryItem(int itemId) {
    GameEngine* engine = g_GameEngine_0046a6ec;
    SCI_Inventory* inventory;
    if (engine == 0) return 0;
    inventory = (SCI_Inventory*)engine->FindHandlerInList(0x1E);
    if (inventory != 0 && inventory->FindItemInList(itemId) != 0) return 1;
    return g_SelectedItem_0046a6e4 != 0 && g_SelectedItem_0046a6e4->itemId == itemId;
}


// Original pickups use OBJ flags as "picked up" markers. Dialogues and
// combat can unfortunately mistake those markers for AP item ownership.
// Temporarily substitute the real inventory state ONLY while the target
// script is being parsed. The old OBJ value must be restored so the vanilla
// pickup remains an Archipelago location check.
// -1 means no override was applied (unbound game or missing flag).
int APBridgeBeginItemEvidence(int itemId, const char* flagName) {
    if (!s_bound || !s_ready || s_worldConflict || flagName == 0) return -1;
    GameState* gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0) return -1;
    int idx = gs->FindState(const_cast<char*>(flagName));
    if (idx < 0 || idx >= gs->maxStates) return -1;
    int previous = gs->stateValues[idx];
    gs->stateValues[idx] = APBridgeHasInventoryItem(itemId) ? 1 : 0;
    return previous;
}
void APBridgeEndItemEvidence(int previousValue, const char* flagName) {
    if (previousValue < 0 || flagName == 0) return;
    GameState* gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0) return;
    int idx = gs->FindState(const_cast<char*>(flagName));
    if (idx >= 0 && idx < gs->maxStates)
        gs->stateValues[idx] = previousValue;
}


void APBridgeLogMaintenanceSource(int apGranted, int apReturning) {
    if (!s_bound || !s_ready || s_worldConflict) return;
    APBridgeAppend("debug.log", apGranted ? "AP_KEY_SOURCE GRANT" :
                   apReturning ? "AP_KEY_SOURCE RETURN" :
                                 "AP_KEY_SOURCE PICKUP");
}

// Called only from the school menu. Indices are Peter=0, Susan=1,
// Duncan=2 as proven by SKOOL.MIS CHAR entries and the existing bridge.
// Outside an active AP session, preserve vanilla character selection.
int APBridgeCharacterUnlocked(int characterIndex) {
    if (characterIndex < 0 || characterIndex > 2) return 0;
    if (!s_bound || !s_ready || s_worldConflict) return 1;
    return (s_charUnlockMask & (1 << characterIndex)) != 0;
}

// Prevent the Go button from using a previously selected locked character,
// including when a save was loaded before virtual unlock receipts replay.
int APBridgeCurrentCharacterUnlocked() {
    GameState* gs = g_GameState_0046aa30;
    int stateIndex = g_PeriodStateIdx_0046cb90;
    if (gs == 0 || gs->stateValues == 0 || stateIndex < 0 ||
        stateIndex >= gs->maxStates) return 0;
    return APBridgeCharacterUnlocked(gs->stateValues[stateIndex]);
}

// The original Choice Menu refuses all portrait clicks after NUM_ACTIONS
// becomes nonzero. If a save or a scripted transition has already committed
// a locked character to the remaining action, blocking Go would strand them.
// This is a narrow fail-safe. It never permits choosing a locked portrait.
int APBridgeCurrentCharacterCanGo() {
    if (APBridgeCurrentCharacterUnlocked()) return 1;
    GameState* gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0) return 0;
    int idx = gs->FindState("NUM_ACTIONS");
    if (idx < 0 || idx >= gs->maxStates) return 0;
    return gs->stateValues[idx] > 0;
}

// At the start of a free Choice Menu (no action committed), an old save may
// point KID to a character not unlocked in AP. Select the first AP-unlocked
// character whom vanilla rules ALSO allow us to select. Do this BEFORE
// SKOOL.MIS is parsed, so sprites, sounds and room options see the new KID.
// Do not change KID after the school has committed NUM_ACTIONS > 0.
void APBridgePrepareChoiceMenu() {
    if (!s_bound || !s_ready || s_worldConflict) return;
    GameState* gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0) return;
    int kidIdx = g_PeriodStateIdx_0046cb90;
    if (kidIdx < 0 || kidIdx >= gs->maxStates) return;
    if (APBridgeCharacterUnlocked(gs->stateValues[kidIdx])) return;
    int actionsIdx = gs->FindState("NUM_ACTIONS");
    if (actionsIdx < 0 || actionsIdx >= gs->maxStates ||
        gs->stateValues[actionsIdx] != 0) return;

    const char* prefixes[3] = {"P", "S", "D"};
    const char* suffixes[3] = {"_IN_DETENTION", "_DETENTION_SUSPENSION", "_CAPTURED"};
    for (int candidate = 0; candidate < 3; ++candidate) {
        if (!APBridgeCharacterUnlocked(candidate)) continue;
        int values[3];
        int valid = 1;
        for (int flag = 0; flag < 3; ++flag) {
            char stateName[48];
            sprintf(stateName, "%s%s", prefixes[candidate], suffixes[flag]);
            int idx = gs->FindState(stateName);
            if (idx < 0 || idx >= gs->maxStates) { valid = 0; break; }
            values[flag] = gs->stateValues[idx];
        }
        // The same predicates used by the vanilla char_click path.
        if (!valid || values[0] != 0 || values[1] > 2 || values[2] != 0)
            continue;
        gs->stateValues[kidIdx] = candidate;
        APBridgeAppend("debug.log", "AP_CHOICE_MENU_RESELECT_UNLOCKED");
        return;
    }
}

// Virtual Progressive Period item (bridge ID 29). First school period is
// free; each received copy permits one additional period, up to period 20.
int APBridgeMaxPeriod() {
    int count = s_progressivePeriods;
    if (count < 0) count = 0;
    if (count > 19) count = 19;
    return 1 + count;
}

// Called at the beginning of SC_Detention::ResetAnimations, before either
// MOVE_TO_NEXT_PERIOD or PERIOD_N can run. No changed story flags here.
// Return 1 for vanilla/offline games, or when the next period is unlocked.
int APBridgeCanEnterNextPeriod() {
    if (!s_bound || !s_ready || s_worldConflict) return 1;
    GameState* gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0) return 1;
    int index = gs->FindState("PERIOD");
    if (index < 0 || index >= gs->maxStates) return 1;
    int period = gs->stateValues[index];
    if (period < 1 || period >= 20) return 1;
    if (period + 1 <= APBridgeMaxPeriod()) return 1;
    if (s_lastPeriodBlocked != period) {
        char entry[100];
        sprintf(entry, "AP_PERIOD_LOCKED current=%d next=%d max=%d", period,
                period + 1, APBridgeMaxPeriod());
        APBridgeAppend("debug.log", entry);
        s_lastPeriodBlocked = period;
    }
    return 0;
}

// Source-backed state transitions from GAMESTAT.MIS and associated ROOM*.MIS.
// This watcher is intentionally conservative: baseline state at start/load is
// not awarded as newly completed. Pending native forcefield/endgame handlers
// are excluded rather than approximated by unrelated flags.
static void APBridgeEvent(int id) {
    if (id <= 0 || id >= 1100 || s_sentEvent[id]) return;
    s_sentEvent[id] = 1;
    APBridgeSendCheck(id);
}
// Native handler focus-change events not represented by reliable GAMESTAT flags.
// The original scripts use handler 65 for duct navigation, and the successful
// Peter/Duncan/Susan finales all queue cinematic 3202 or 3203 FROM DETENTION 4.
// Failed finales use separate cutscenes and DETENTION 5.
void APBridgeOnFocusChange(int toType, int toValue, int fromType, int fromValue) {
    GameState* gs;
    int kid;
    // Record all focus transitions, not just cinematics. A puzzle may report
    // success by returning to a room rather than setting a persistent flag.
    GameState* traceGsAll = g_GameState_0046aa30;
    if (traceGsAll != 0 && traceGsAll->stateValues != 0 &&
        traceGsAll->maxStates > 99) {
        char entry[180];
        sprintf(entry, "AP_TRACE_FOCUS to=%d/%d from=%d/%d period=%d kid=%d",
                toType, toValue, fromType, fromValue,
                traceGsAll->stateValues[60], traceGsAll->stateValues[99]);
        APBridgeTraceLine(entry);
    }
    // Period 1 playtesting shows the device discovery plays before the period gate.
    // CIN7810 follows the fire alarm and starts from detention (handler 44).
    // Only this transition in period 1 can award the discovery check.
    // Confirm the cinematic-to-scene match in the next game test.
    if (toType == 3) {
        GameState* traceGs = g_GameState_0046aa30;
        if (traceGs != 0 && traceGs->stateValues != 0) {
            int periodIdx = traceGs->FindState("PERIOD");
            if (periodIdx >= 0 && periodIdx < traceGs->maxStates &&
                traceGs->stateValues[periodIdx] >= 1 &&
                traceGs->stateValues[periodIdx] <= 2) {
                char entry[130];
                sprintf(entry,
                    "AP_EARLY_CINEMATIC id=%d period=%d from=%d/%d",
                    toValue, traceGs->stateValues[periodIdx],
                    fromType, fromValue);
                APBridgeAppend("debug.log", entry);
                if (toValue == 7810 && fromType == 44 && fromValue == 1 &&
                    traceGs->stateValues[periodIdx] == 1 &&
                    s_bound && s_ready && !s_worldConflict) {
                    // Avoid a second award/log if the period is replayed.
                    if (!s_sentEvent[1020]) {
                        APBridgeEvent(1020); // Hall - Found Alien Device
                        APBridgeAppend("debug.log", "AP_ALIEN_DEVICE_CHECK_CIN7810");
                    }
                }
            }
        }
    }
    if (toType == 65 && toValue == 1) {
        // Practice-room challenges are not ordinary school exploration.
        gs = g_GameState_0046aa30;
        if (gs != 0 && gs->stateValues != 0 && gs->maxStates > 112 &&
            gs->stateValues[112] == 0 &&
            gs->stateValues[60] >= 1 && gs->stateValues[60] <= 20)
            APBridgeEvent(1022);
        return;
    }
    if (toType != 3 || (toValue != 3202 && toValue != 3203) ||
        fromType != 44 || fromValue != 4) return;
    gs = g_GameState_0046aa30;
    if (gs == 0 || gs->stateValues == 0 || gs->maxStates <= 99) return;
    kid = gs->stateValues[99];
    if (kid == 0) APBridgeEvent(1045);      // Peter - PZ_CRYSTAL
    else if (kid == 2) APBridgeEvent(1046); // Duncan - CB_GAUNTLET
    else if (kid == 1) APBridgeEvent(1047); // Susan - QUEST26
}

static void APBridgeWatchStates() {
    GameState* gs = g_GameState_0046aa30;
    int i; int* now;
    if (gs == 0 || gs->stateValues == 0 || gs->maxStates < 450) return;
    now = gs->stateValues;
    if (!s_haveBaseline) {
        for (i=0; i<450; ++i) s_lastState[i] = now[i];
        s_haveBaseline = 1;
        return;
    }
    // Each newly entered period is an AP location (1047 + period number).
    // A completed location is deduplicated across New Games by APBridgeEvent.
    if (s_lastState[60] != now[60] && now[60] >= 1 && now[60] <= 20)
        APBridgeEvent(1047 + now[60]);
    // Alien Device location 1020 is sent by CIN7810 before the period gate.
    // It must not depend on the blocked PERIOD 1 -> 2 state change.
    if (s_lastState[107] == 0 && now[107] != 0) APBridgeEvent(1021); // Yahoo puzzle
    // Duct entry is emitted on focus transition into COMBAT_DUCTNAV (handler 65).
    // BEEN_IN_DUCTWORK is set after returning to certain rooms, too late.
    // ROOM24 sets S_BEEN_SHIP unconditionally (despite the Susan prefix).
    if (s_lastState[172] == 0 && now[172] != 0) APBridgeEvent(1023);
    if (s_lastState[102] == 0 && now[102] != 0) APBridgeEvent(1024); // fan
    if (s_lastState[35] != 0 && now[35] == 0) APBridgeEvent(1025); // rats
    if (s_lastState[101] != 1 && now[101] == 1) APBridgeEvent(1026); // left bridge
    if (s_lastState[101] != 2 && now[101] == 2) APBridgeEvent(1027); // right bridge
    if (s_lastState[109] == 0 && now[109] != 0) APBridgeEvent(1028); // slime
    if (s_lastState[216] == 0 && now[216] != 0) APBridgeEvent(1029);
    if (s_lastState[215] == 0 && now[215] != 0) APBridgeEvent(1030);
    if (s_lastState[232] == 0 && now[232] != 0) APBridgeEvent(1031);
    if (s_lastState[227] == 0 && now[227] != 0) APBridgeEvent(1032);
    if (s_lastState[235] == 0 && now[235] == now[92] && now[235] >= 1 && now[235] <= 3)
        APBridgeEvent(1033);
    if (s_lastState[104] == 0 && now[104] != 0) APBridgeEvent(1034);
    if (s_lastState[105] == 0 && now[105] != 0) APBridgeEvent(1035);
    if (s_lastState[106] == 0 && now[106] != 0) APBridgeEvent(1036);
    if (s_lastState[173] == 0 && now[173] != 0) APBridgeEvent(1037);
    if ((s_lastState[129] == 0 && now[129] != 0) ||
        (s_lastState[188] == 0 && now[188] != 0)) APBridgeEvent(1038);
    // Captivity flags 93..98. The game may rescue from a spaceship door by
    // changing 1 -> 0; prevents are handled through their transient pod win.
    for (i=93; i<=98; ++i)
        if (s_lastState[i] == 1 && now[i] == 0) APBridgeEvent(1039 + i-93);
    // The pod win result is only meaningful when associated with a student.
    // DETENTION.MIS HANDLEPOD is invoked on entering periods 8/12/15/17,
    // so the minigame is performed in periods 7/11/14/16 respectively.
    if (s_lastState[108] == 0 && now[108] == 1) {
        if (now[60] == 7) APBridgeEvent(1041);  // Wendy
        else if (now[60] == 11) APBridgeEvent(1042); // Jack
        else if (now[60] == 14) APBridgeEvent(1043); // Stacy
        else if (now[60] == 16) APBridgeEvent(1044); // Orville
    }
    // Log actual state changes, including those performed directly by C++
    // handlers instead of GAMESTAT.MIS actions. The normal check logic above
    // still runs first, entirely unchanged.
    for (i=0; i<450; ++i) {
        if (s_lastState[i] != now[i]) {
            char entry[180];
            sprintf(entry, "AP_TRACE_DELTA idx=%d old=%d new=%d period=%d kid=%d",
                    i, s_lastState[i], now[i], now[60], now[99]);
            APBridgeTraceLine(entry);
        }
        s_lastState[i] = now[i];
    }
}

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
    // Establish an initial snapshot. Loading an older save is not by itself
    // a new accomplishment; only changes occurring during gameplay count.
    s_haveBaseline = 0;
    APBridgeWatchStates();
    // First entry may be missed by transition watcher because READY takes a
    // baseline snapshot; mark the actually loaded/started period once.
    {
        GameState* gs = g_GameState_0046aa30;
        if (gs != 0 && gs->stateValues != 0) {
            int index = gs->FindState("PERIOD");
            if (index >= 0 && index < gs->maxStates &&
                gs->stateValues[index] >= 1 && gs->stateValues[index] <= 20)
                APBridgeEvent(1047 + gs->stateValues[index]);
        }
    }
    sprintf(line, "READY %s %s %d", s_world, s_session, s_generation);
    APBridgeAppend("events.log", line);
}
// Invalidate delivery acknowledgments whenever the original game replaces
// its inventory. New Game needs a separate event because it does NOT use
// GameEngine::Serialize, unlike Load Game.
static void APBridgeResetGeneration(const char* eventType) {
    char line[200];
    APBridgeEnsureSession();
    if (!s_bound || s_worldConflict) return;
    ++s_generation;
    s_ready = 0;
    s_haveBaseline = 0;
    s_charUnlockMask = 1;
    s_progressivePeriods = 0;
    s_lastPeriodBlocked = -1;
    s_traceLines = 0;  // Fresh trace budget per New Game or Load.
    s_queuedGrantId = 0;
    s_queuedSequence = 0;
    s_activeGrantId = 0;
    s_activeSequence = 0;
    sprintf(line, "%s %s %s %d", eventType, s_world, s_session, s_generation);
    APBridgeAppend("events.log", line);
}
void APBridgeStoryReloaded() {
    APBridgeResetGeneration("LOAD");
    APBridgeAppend("debug.log", "GAME_LOAD_COMPLETE_V5");
    // GameEngine::Serialize finished loading all the handlers.
    APBridgeGameplayReady();
}
void APBridgeNewGame() {
    // Called after InitNewGame has removed old handlers and created new state.
    // SchoolMenu::Init will emit READY after the new school scene begins.
    APBridgeResetGeneration("NEWGAME");
    APBridgeAppend("debug.log", "NEW_GAME_STATE_RESET_V7_2");
}
int APBridgeInterceptRecipeFlag(int stateId, int instruction) {
    // Original .MIS scripts trigger knowledge through GameState::SetFromAction.
    // Chat QUEST19 questions: speech=2507, taste=2511, sound=2522.
    // ROOM19 SEARCHSCREEN21_STATIC uses SET 1 rather than TURNON for water.
    int locationId = 0;
    if (instruction == 0xF) {
        if (stateId == 77) locationId = 1005;
        else if (stateId == 78) locationId = 1009;
        else if (stateId == 79) locationId = 1011;
    } else if (stateId == 76 && instruction == 0x13) {
        locationId = 1013;
    }
    if (locationId == 0) return 0;
    APBridgeSendCheck(locationId);
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
    if (s_bound && s_ready && !s_worldConflict) APBridgeWatchStates();
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
               itemId < 1 || itemId > 31 || seq <= 0) {
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
    if (itemId == 29) {
        // Each receipt is a distinct copy of the same AP progression item.
        // Replaying receipts reconstructs the count after a new week or load.
        int alreadyPresent = s_progressivePeriods >= 19;
        s_activeGrantId = s_queuedGrantId;
        s_activeSequence = s_queuedSequence;
        s_queuedGrantId = 0;
        s_queuedSequence = 0;
        if (!alreadyPresent) ++s_progressivePeriods;
        s_lastPeriodBlocked = -1;
        APBridgeGrantFinished(itemId, alreadyPresent);
        {
            char entry[100];
            sprintf(entry, "AP_PROGRESSIVE_PERIOD count=%d max=%d",
                    s_progressivePeriods, APBridgeMaxPeriod());
            APBridgeAppend("debug.log", entry);
        }
        return;
    }
    if (itemId >= 26 && itemId <= 28) {
        // Virtual character unlocks: no inventory item or original story flag.
        // Same sequence acknowledgment as physical items, so replays on Load
        // and New Game are naturally handled by the existing bridge protocol.
        int bit = 1 << (itemId - 26);
        int alreadyPresent = (s_charUnlockMask & bit) != 0;
        s_activeGrantId = s_queuedGrantId;
        s_activeSequence = s_queuedSequence;
        s_queuedGrantId = 0;
        s_queuedSequence = 0;
        s_charUnlockMask |= bit;
        APBridgeGrantFinished(itemId, alreadyPresent);
        APBridgeAppend("debug.log", "AP_CHARACTER_UNLOCK_APPLIED");
        return;
    }
    if (itemId >= 22 && itemId <= 25) {
        // Virtual AP knowledge items. GameState indexes are defined in
        // GAMESTAT.MIS: Water=76, Speech=77, Taste=78, Sound=79.
        int stateId = (itemId == 22 ? 77 :
                      itemId == 23 ? 78 : itemId == 24 ? 79 : 76);
        GameState* gs = g_GameState_0046aa30;
        if (gs == 0 || gs->stateValues == 0 || gs->maxStates <= stateId) {
            s_queuedGrantId = 0;
            s_queuedSequence = 0;
            APBridgeAppend("debug.log", "KNOWLEDGE_GAMESTATE_NOT_READY");
            return;
        }
        s_activeGrantId = s_queuedGrantId;
        s_activeSequence = s_queuedSequence;
        s_queuedGrantId = 0;
        s_queuedSequence = 0;
        {
            int alreadyPresent = gs->stateValues[stateId] != 0;
            gs->stateValues[stateId] = 1;
            // Let the original game perform all three AUTOCOMBINE recipes.
            // Water uses its flag directly and has no inventory recipe.
            if (stateId != 76) {
                GameEngine* engine = g_GameEngine_0046a6ec;
                if (engine != 0) {
                    SCI_Inventory* inv = (SCI_Inventory*)engine->FindHandlerInList(0x1E);
                    if (inv != 0) inv->DisplayPanels(0);
                }
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
    newgame_path, newgame, newgame_eol = load('SC_DuctNav.cpp')
    pods_path, pods, pods_eol = load('SC_Pods.cpp')
    detention_path, detention, detention_eol = load('SC_Detention.cpp')
    question_path, question, quest_eol = load('SC_Question.cpp')
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
        '        // The engine also INSERTs an item to return it after USE/PUT BACK.\n'
        '        // This is NOT a fresh location pickup. Let it return normally.\n'
        '        int apReturning = g_SelectedItem_0046a6e4 != 0 &&\n'
        '                          g_SelectedItem_0046a6e4->itemId == itemId;\n' 
        '        // Diagnose any AP grant / pickup confusion for Maintenance Key.\n'
        '        // Logging is source-specific and does not itself award a check.\n'
        '        if (itemId == 3) APBridgeLogMaintenanceSource(apGranted, apReturning);\n'
        '        // Real pickups generate location checks; AP grants own inventory separately.\n'
        '        if (!apGranted && !apReturning && (itemId == 5 || itemId == 7 || itemId == 2 || itemId == 8 ||\n'
        '                           itemId == 9 || itemId == 6 || itemId == 4 ||\n'
        '                           itemId == 17 || itemId == 10 ||\n'
        '                           itemId == 3 || itemId == 14 || itemId == 21 ||\n'
        '                           itemId == 12 || itemId == 11 || itemId == 1)) {\n'
        '            T_Object* pickup = (T_Object*)FindItem(itemId);\n'
        '            if (pickup != 0) pickup->Reset();\n'
        '            int checkId = itemId == 5 ? 1001 : itemId == 7 ? 1002 :\n'
        '                          itemId == 2 ? 1003 : itemId == 8 ? 1004 :\n'
        '                          itemId == 9 ? 1006 : itemId == 6 ? 1007 :\n'
        '                          itemId == 4 ? 1008 : itemId == 17 ? 1010 :\n'
        '                          itemId == 10 ? 1012 : itemId == 3 ? 1014 :\n'
        '                          itemId == 14 ? 1015 : itemId == 21 ? 1016 :\n'
        '                          itemId == 12 ? 1017 : itemId == 11 ? 1018 : 1019;\n'
        '            APBridgeSendCheck(checkId);\n'
        '            break;\n'
        '        }\n'
        '        // A finished native test already contains the consumed AP ingredients.\n'
        '        if (apGranted && (((itemId == 2 || itemId == 7 || itemId == 8) &&\n'
        '                           FindItemInList(18) != 0) ||\n'
        '                          ((itemId == 9 || itemId == 6 || itemId == 4) &&\n'
        '                           FindItemInList(19) != 0) ||\n'
        '                          ((itemId == 5 || itemId == 17) &&\n'
        '                           FindItemInList(20) != 0) ||\n'
        '                          (itemId == 10 && FindItemInList(13) != 0))) {\n'
        '            APBridgeGrantFinished(itemId, 1);\n'
        '            break;\n'
        '        }\n'
        '        // SOUND_TEST in ROOM07 sets flag 230 before deleting finished item 20.\n'
        "        // Don't resurrect a one-use sound test on a post-use save load.\n"
        '        if (apGranted && (itemId == 5 || itemId == 17) &&\n'
        '            g_GameState_0046aa30 != 0 &&\n'
        '            g_GameState_0046aa30->stateValues != 0 &&\n'
        '            g_GameState_0046aa30->maxStates > 230 &&\n'
        '            g_GameState_0046aa30->stateValues[230] != 0) {\n'
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
        '        if (!apGranted && !apReturning) item->Reset();', 'Inventory pickup flag')
    inventory = change_once(inventory,
        '        DisplayPanels(itemId);\n        break;\n    }\n    case 0x18:',
        '        DisplayPanels(itemId);\n'
        '        if (apGranted) APBridgeGrantFinished(itemId, 0);\n'
        '        break;\n    }\n    case 0x18:', 'Inventory receipt acknowledged')
    pods = change_once(pods, '#include "globals.h"\n',
        '#include "globals.h"\n#include "APBridge.h"\n', 'Pod minigame bridge include')
    pods = change_once(pods,
        '    ParseFile(this, "mis\\\\cb_Pods.mis", (char*)0);\n'
        '    InitCombatScreen(g_CombatEngine_0046ae78);',
        '    // cb_pods.mis chooses visible or invisible attack orbs from OBJ011.\n'
        '    // Mirror actual AP inventory in BOTH directions.\n'
        '    int apGunBefore = APBridgeBeginItemEvidence(11, "OBJ011");\n'
        '    ParseFile(this, "mis\\\\cb_Pods.mis", (char*)0);\n'
        '    InitCombatScreen(g_CombatEngine_0046ae78);\n'
        '    APBridgeEndItemEvidence(apGunBefore, "OBJ011");',
        'AP equipment pod parse')
    question = change_once(question, '#include "GameState.h"\n',
        '#include "GameState.h"\n#include "APBridge.h"\n',
        'Reporter dialogue bridge include')
    question = change_once(question,
        '    ParseFile(this, questFile, "[QUESTION%d]", questionId);',
        '    // Only quest06.mis contains the reporter evidence questions.\n'
        '    // Do not change OBJ021 globally: it controls vanilla list pickup.\n'
        '    int listFlagBefore = -1;\n'
        '    if (strstr(questFile, "quest06.mis") != 0)\n'
        '        listFlagBefore = APBridgeBeginItemEvidence(21, "OBJ021");\n'
        '    ParseFile(this, questFile, "[QUESTION%d]", questionId);\n'
        '    APBridgeEndItemEvidence(listFlagBefore, "OBJ021");',
        'Reporter evidence list script scope')
    detention = change_once(detention, '#include "GameState.h"\n',
                            '#include "GameState.h"\n#include "APBridge.h"\n',
                            'Detention bridge include')
    detention = change_once(detention,
        'void SC_Detention::ResetAnimations() {\n    GameState* gs;',
        'void SC_Detention::ResetAnimations() {\n'
        '    // Prototype gate: do not increment PERIOD or fire a locked period\'s events.\n'
        '    // Existing caller handles returning to the current period menu.\n'
        '    if (!APBridgeCanEnterNextPeriod()) return;\n'
        '    GameState* gs;',
        'Progressive Period pre-transition gate')
    state = change_once(state, '#include "GameState.h"\n',
        '#include "GameState.h"\n#include "APBridge.h"\n', 'Gamestate bridge include')
    state = change_once(state,
        '    switch (action->instruction) {\n',
        '    if (APBridgeInterceptRecipeFlag(idx, action->instruction)) return;\n'
        '    APBridgeTraceStateAction(idx, action->instruction);\n'
        '    switch (action->instruction) {\n', 'Recipe flag interception')
    # Hook actual focus-change completion, not a late/reused state flag.
    # Both success endings and normal duct entry route through this handler.
    engine = change_once(engine,
        '    handler = (Handler*)m_activeHandler;\n'
        '    if (handler != 0) {\n'
        '        handler->Init(msg);\n'
        '    } else {\n'
        '        ShowError("missing modual %d", action->addressType);\n'
        '    }\n'
        '}\n',
        '    handler = (Handler*)m_activeHandler;\n'
        '    if (handler != 0) {\n'
        '        handler->Init(msg);\n'
        '        APBridgeOnFocusChange(action->addressType, action->addressValue,\n'
        '                              action->fromType, action->fromValue);\n'
        '    } else {\n'
        '        ShowError("missing modual %d", action->addressType);\n'
        '    }\n'
        '}\n',
        'Focus transition and winning ending notification')
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
    # Normalize AP-locked initial KID before the original script builds menu UI.
    # NUM_ACTIONS>0 means the original game has already locked character switching.
    school = change_once(school,
        '    ParseFile(this, "mis\\\\skool.mis", "[DURINGSCHOOLMENU]");',
        '    APBridgePrepareChoiceMenu();\n'
        '    ParseFile(this, "mis\\\\skool.mis", "[DURINGSCHOOLMENU]");',
        'Choice Menu initial locked-character recovery')
    # Character access is checked at the click itself, before original story
    # eligibility tests. This has no effect on KID/P_CAPTURED/etc. values.
    school = change_once(school,
        'char_click:\n    // Character was clicked\n',
        'char_click:\n    // AP character restrictions are menu-only, not story flags.\n'
        '    if (!APBridgeCharacterUnlocked(i)) goto check_options;\n'
        '    // Character was clicked\n',
        'Character unlock selection gate')
    school = change_once(school,
        'check_go:\n    if (selectedOption <= -1) goto check_back;\n',
        'check_go:\n    // Never permit a locked selected character to enter a room.\n'
        '    if (!APBridgeCurrentCharacterCanGo()) goto check_back;\n'
        '    if (selectedOption <= -1) goto check_back;\n',
        'Character unlock Go-button forced-continuation gate')
    # Keep the Go button's visible active state consistent with the click gate.
    # Forced, previously committed characters are allowed through as a fail-safe.
    school = change_once(school,
        'selectedOption > -1 && IsCharacterActive() != 0)',
        'selectedOption > -1 && IsCharacterActive() != 0 &&\n'
        '        APBridgeCurrentCharacterCanGo())',
        'Character unlock Go-button forced-continuation visual state')
    # The save/load menu module creates a completely fresh GameState and
    # inventory during New Game; it never calls GameEngine::Serialize.
    newgame = change_once(newgame, '#include "GameEngine.h"\n',
        '#include "GameEngine.h"\n#include "APBridge.h"\n', 'New Game bridge include')
    newgame = change_once(newgame,
        '            InitNewGame();\n            SendGameMessage(3, 10, g_PendingAction_00472d58.addressType,',
        '            InitNewGame();\n            APBridgeNewGame();\n'
        '            SendGameMessage(3, 10, g_PendingAction_00472d58.addressType,',
        'New Game completed notification')
    engine_path.write_bytes(engine.replace('\n', eng_eol).encode('utf-8'))
    school_path.write_bytes(school.replace('\n', school_eol).encode('utf-8'))
    inventory_path.write_bytes(inventory.replace('\n', inv_eol).encode('utf-8'))
    state_path.write_bytes(state.replace('\n', state_eol).encode('utf-8'))
    newgame_path.write_bytes(newgame.replace('\n', newgame_eol).encode('utf-8'))
    pods_path.write_bytes(pods.replace('\n', pods_eol).encode('utf-8'))
    detention_path.write_bytes(detention.replace('\n', detention_eol).encode('utf-8'))
    question_path.write_bytes(question.replace('\n', quest_eol).encode('utf-8'))
    bridge_h.write_text(HEADER, encoding='utf-8')
    bridge_cpp.write_text(CPP, encoding='utf-8')
    print('AP bridge v9.2 unified event trace applied (all existing v9.1 logic preserved)')


if __name__ == '__main__':
    main()
